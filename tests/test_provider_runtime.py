import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from tau_ai import AnthropicProvider, OpenAICodexProvider, OpenAICompatibleProvider
from tau_coding import provider_runtime
from tau_coding.credentials import FileCredentialStore, OAuthCredential
from tau_coding.oauth_registry import (
    register_oauth_provider,
    reset_oauth_providers,
    unregister_oauth_provider,
)
from tau_coding.oauth_types import OAuthLoginCallbacks, OAuthProvider, OAuthRuntimeAuth
from tau_coding.provider_config import (
    AnthropicProviderConfig,
    OpenAICodexProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfigError,
    ProviderModelMetadata,
    provider_config_from_catalog_entry,
    resolve_startup_thinking_level,
)
from tau_coding.provider_runtime import (
    OAuthRuntimeCredentialResolver,
    OpenAICodexCredentialResolver,
    _file_refresh_lock,
    create_model_provider,
)

_GATEWAY_NAME = "claude-gateway"


def _gateway_config() -> OpenAICompatibleProviderConfig:
    """A custom openai-compatible gateway with one Anthropic-protocol model."""
    return OpenAICompatibleProviderConfig(
        name=_GATEWAY_NAME,
        base_url="https://gateway.test/v1",
        api_key_env="CLAUDE_GATEWAY_API_KEY",
        credential_name=_GATEWAY_NAME,
        models=("claude-haiku-4.5", "gpt-5.4"),
        default_model="claude-haiku-4.5",
        model_metadata={
            "claude-haiku-4.5": ProviderModelMetadata(
                api="anthropic-messages",
                input=("text", "image"),
            ),
        },
    )


class _GatewayOAuthProvider:
    """Minimal registered OAuth provider backing the gateway fixtures."""

    id = _GATEWAY_NAME
    name = _GATEWAY_NAME
    flow_kinds = ("device_code",)

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url

    async def login(self, _callbacks: OAuthLoginCallbacks) -> OAuthCredential:
        raise AssertionError("not used")

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        raise AssertionError("not used")

    def runtime_auth(self, credential: OAuthCredential) -> OAuthRuntimeAuth:
        return OAuthRuntimeAuth(api_key=credential.access, base_url=self.base_url)


_CROSS_PROCESS_REFRESH_SCRIPT = """
import asyncio
import sys
import time
from pathlib import Path

from tau_coding import provider_runtime
from tau_coding.credentials import FileCredentialStore, OAuthCredential
from tau_coding.provider_config import OpenAICodexProviderConfig
from tau_coding.provider_runtime import OpenAICodexCredentialResolver

store_path = Path(sys.argv[1])
ready_dir = Path(sys.argv[2])
refresh_log = Path(sys.argv[3])
result_path = Path(sys.argv[4])
index = sys.argv[5]


async def fake_refresh(refresh_token: str) -> OAuthCredential:
    # Emulate a rotating authorization server: the first use of a refresh
    # token succeeds and records the spend; any later use is rejected.
    with refresh_log.open("a+", encoding="utf-8") as handle:
        handle.seek(0)
        spent = handle.read()
        handle.write(refresh_token + "\\n")
        handle.flush()
    if refresh_token in spent:
        raise RuntimeError(f"refresh_token_reused: {refresh_token}")
    await asyncio.sleep(1.0)
    return OAuthCredential(
        access="access-2",
        refresh="refresh-2",
        expires=9999999999999,
        account_id="acct-2",
    )


provider_runtime.refresh_openai_codex_token = fake_refresh

# Rendezvous: both processes call the resolver at the same time, so an
# unlocked implementation reads the stale credential in both before either
# refreshes.
(ready_dir / f"ready-{index}").write_text("")
deadline = time.monotonic() + 30
while len(list(ready_dir.glob("ready-*"))) < 2:
    if time.monotonic() > deadline:
        raise SystemExit("timed out waiting for the sibling process")
    time.sleep(0.01)

resolver = OpenAICodexCredentialResolver(
    OpenAICodexProviderConfig(),
    credential_store=FileCredentialStore(store_path),
)
try:
    credentials = asyncio.run(resolver())
except BaseException as exc:
    result_path.write_text(f"ERROR: {type(exc).__name__}: {exc}")
    raise
result_path.write_text(f"{credentials.access_token}\\n{credentials.account_id}")
"""

_SAME_STORE_CONCURRENT_REFRESH_SCRIPT = """
import asyncio
import sys
from pathlib import Path

from tau_coding import provider_runtime
from tau_coding.credentials import FileCredentialStore, OAuthCredential
from tau_coding.provider_config import OpenAICodexProviderConfig
from tau_coding.provider_runtime import OpenAICodexCredentialResolver

store_path = Path(sys.argv[1])
result_path = Path(sys.argv[2])


async def fake_refresh(refresh_token: str) -> OAuthCredential:
    # Slow network refresh: long enough that the second task, past the
    # per-name lock, reaches the file lock while the first still holds it.
    await asyncio.sleep(1.0)
    return OAuthCredential(
        access=f"new-{refresh_token}",
        refresh=f"fresh-{refresh_token}",
        expires=9999999999999,
        account_id=f"acct-{refresh_token}",
    )


provider_runtime.refresh_openai_codex_token = fake_refresh

store = FileCredentialStore(store_path)
store.set_oauth(
    "codex-a",
    OAuthCredential(access="old-a", refresh="refresh-a", expires=1, account_id="acct-a"),
)
store.set_oauth(
    "codex-b",
    OAuthCredential(access="old-b", refresh="refresh-b", expires=1, account_id="acct-b"),
)


async def main() -> None:
    def resolver(credential_name: str) -> OpenAICodexCredentialResolver:
        return OpenAICodexCredentialResolver(
            OpenAICodexProviderConfig(credential_name=credential_name),
            credential_store=store,
        )

    async def refresh(credential_name: str) -> str:
        credentials = await resolver(credential_name)()
        return credentials.access_token

    async with asyncio.timeout(20):
        results = await asyncio.gather(refresh("codex-a"), refresh("codex-b"))
    result_path.write_text("\\n".join(results))


asyncio.run(main())
"""


def test_create_model_provider_returns_openai_codex_provider(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")

    provider = create_model_provider(
        OpenAICodexProviderConfig(),
        credential_store=store,
    )

    assert isinstance(provider, OpenAICodexProvider)


def test_create_model_provider_uses_codex_model_image_capability(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    config = provider_config_from_catalog_entry("openai-codex")

    vision_provider = create_model_provider(
        config,
        credential_store=store,
        model="gpt-5.6-sol",
    )
    text_provider = create_model_provider(
        config,
        credential_store=store,
        model="gpt-5.3-codex-spark",
    )

    assert isinstance(vision_provider, OpenAICodexProvider)
    assert isinstance(text_provider, OpenAICodexProvider)
    assert vision_provider._config.supports_images is True
    assert text_provider._config.supports_images is False


def test_direct_openai_runtime_enables_responses_cache_affinity(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("openai", "sk-test")

    provider = create_model_provider(
        provider_config_from_catalog_entry("openai"),
        credential_store=store,
        model="gpt-5.4",
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.compat["supportsPromptCacheKey"] is True
    assert provider._config.compat["sendSessionAffinityHeaders"] is True
    assert provider._config.compat["sessionAffinityFormat"] == "openai"


def test_huggingface_runtime_pins_backing_provider_with_model_alias(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("huggingface", "hf-test")
    config = provider_config_from_catalog_entry("huggingface")

    provider = create_model_provider(
        config,
        credential_store=store,
        model="zai-org/GLM-5.2",
        inference_provider="deepinfra",
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.model_aliases == {"zai-org/GLM-5.2": "zai-org/GLM-5.2:deepinfra"}


def test_huggingface_runtime_rejects_policy_suffix(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("huggingface", "hf-test")

    with pytest.raises(ProviderConfigError, match="explicit"):
        create_model_provider(
            provider_config_from_catalog_entry("huggingface"),
            credential_store=store,
            model="zai-org/GLM-5.2",
            inference_provider="fastest",
        )


def test_compatible_gateway_defaults_to_no_openai_cache_affinity(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("together", "gateway-key")

    provider = create_model_provider(
        provider_config_from_catalog_entry("together"),
        credential_store=store,
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.compat["supportsPromptCacheKey"] is False
    assert provider._config.compat["sendSessionAffinityHeaders"] is False


def test_create_model_provider_uses_anthropic_oauth_runtime_auth(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "anthropic",
        OAuthCredential(
            access="anthropic-oauth-access",
            refresh="anthropic-refresh",
            expires=9999999999999,
        ),
    )

    provider = create_model_provider(AnthropicProviderConfig(), credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.bearer_auth is True
    assert provider._config.credential_resolver is not None
    assert provider._config.oauth_system_prompt is not None
    assert provider._config.headers is not None
    assert provider._config.headers["Authorization"] == "Bearer anthropic-oauth-access"
    # Subscription auth is not billed per token, so ask for the 1 hour cache TTL.
    assert provider._config.cache_retention == "long"


def test_anthropic_api_key_auth_keeps_the_default_cache_retention(tmp_path) -> None:
    """1h cache writes cost 2x base, so an API-key user must not get them silently."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("anthropic", "sk-test")

    provider = create_model_provider(AnthropicProviderConfig(), credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "short"


@pytest.mark.parametrize(
    "provider_name",
    ["minimax", "minimax-cn", "fireworks", "vercel-ai-gateway"],
)
def test_anthropic_protocol_gateways_disable_cache_breakpoints(
    provider_name: str,
    tmp_path,
) -> None:
    """Gateways speaking the Anthropic protocol may reject cache_control blocks."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key(provider_name, "gateway-key")
    config = provider_config_from_catalog_entry(provider_name)

    provider = create_model_provider(config, credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "none"


def test_provider_compat_can_re_enable_cache_breakpoints_on_a_gateway(tmp_path) -> None:
    """A gateway proxying real Claude must be able to opt back in without a source edit."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("minimax", "gateway-key")
    config = provider_config_from_catalog_entry("minimax")
    config = replace(config, compat={**config.compat, "supportsCacheControl": True})

    provider = create_model_provider(config, credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "short"
    assert provider._config.cache_control_on_tools is True


def test_provider_compat_can_clamp_the_one_hour_ttl_on_oauth(tmp_path) -> None:
    """The escape hatch if Anthropic ever stops honoring ttl=1h on subscriptions."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "anthropic",
        OAuthCredential(access="a", refresh="r", expires=9999999999999),
    )
    config = replace(AnthropicProviderConfig(), compat={"supportsLongCacheRetention": False})

    provider = create_model_provider(config, credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "short"


def test_provider_compat_can_suppress_only_the_tools_breakpoint(tmp_path) -> None:
    """Some gateways accept cache_control everywhere except inside tool objects."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_api_key("anthropic", "sk-test")
    config = replace(AnthropicProviderConfig(), compat={"supportsCacheControlOnTools": False})

    provider = create_model_provider(config, credential_store=store)

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "short"
    assert provider._config.cache_control_on_tools is False


def test_anthropic_protocol_models_on_openai_compatible_providers_disable_cache_breakpoints(
    tmp_path,
) -> None:
    """A custom gateway speaking the Anthropic protocol gets no cache_control by default."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        _GATEWAY_NAME,
        OAuthCredential(access="gateway-access", refresh="gateway-token", expires=9999999999999),
    )
    register_oauth_provider(cast(OAuthProvider, _GatewayOAuthProvider()))
    try:
        provider = create_model_provider(
            _gateway_config(),
            credential_store=store,
            model="claude-haiku-4.5",
        )
    finally:
        unregister_oauth_provider(_GATEWAY_NAME)
        reset_oauth_providers()

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "none"


def test_model_metadata_compat_enables_cache_breakpoints_on_anthropic_protocol_gateways(
    tmp_path,
) -> None:
    """Per-model compat reaches the openai-compatible Anthropic-protocol branch."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        _GATEWAY_NAME,
        OAuthCredential(access="gateway-access", refresh="gateway-token", expires=9999999999999),
    )
    config = _gateway_config()
    metadata = config.model_metadata["claude-haiku-4.5"]
    config = replace(
        config,
        model_metadata={
            **config.model_metadata,
            "claude-haiku-4.5": replace(
                metadata, compat={**metadata.compat, "supportsCacheControl": True}
            ),
        },
    )

    register_oauth_provider(cast(OAuthProvider, _GatewayOAuthProvider()))
    try:
        provider = create_model_provider(config, credential_store=store, model="claude-haiku-4.5")
    finally:
        unregister_oauth_provider(_GATEWAY_NAME)
        reset_oauth_providers()

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.cache_retention == "long"


def test_create_model_provider_uses_model_max_tokens_for_anthropic_protocol_model(
    tmp_path,
) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        _GATEWAY_NAME,
        OAuthCredential(access="gateway-access", refresh="gateway-token", expires=9999999999999),
    )
    config = _gateway_config()
    metadata = dict(config.model_metadata)
    metadata["claude-haiku-4.5"] = replace(metadata["claude-haiku-4.5"], max_tokens=64_000)
    provider_config = replace(config, model_metadata=metadata)

    register_oauth_provider(cast(OAuthProvider, _GatewayOAuthProvider()))
    try:
        provider = create_model_provider(
            provider_config,
            credential_store=store,
            model="claude-haiku-4.5",
        )
    finally:
        unregister_oauth_provider(_GATEWAY_NAME)
        reset_oauth_providers()

    assert isinstance(provider, AnthropicProvider)
    assert provider._config.max_tokens == 64_000


def test_create_model_provider_uses_oauth_base_url_override(tmp_path) -> None:
    """runtime_auth.base_url replaces the configured base_url for OAuth gateways."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        _GATEWAY_NAME,
        OAuthCredential(access="gateway-access", refresh="gateway-token", expires=9999999999999),
    )
    oauth_provider = _GatewayOAuthProvider(base_url="https://proxy.gateway.test/v1")
    register_oauth_provider(cast(OAuthProvider, oauth_provider))
    try:
        provider = create_model_provider(
            _gateway_config(),
            credential_store=store,
            model="gpt-5.4",
        )
    finally:
        unregister_oauth_provider(_GATEWAY_NAME)
        reset_oauth_providers()

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.base_url == "https://proxy.gateway.test/v1"
    assert provider._config.credential_resolver is not None


def test_create_model_provider_rejects_model_not_declared_for_provider(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    provider_config = OpenAICompatibleProviderConfig(
        name="local",
        models=("qwen",),
        default_model="qwen",
    )

    with pytest.raises(
        ProviderConfigError,
        match="Model is not configured for provider local: llama",
    ):
        create_model_provider(provider_config, credential_store=store, model="llama")


def test_create_model_provider_maps_codex_reasoning_effort_like_pi(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    provider_config = OpenAICodexProviderConfig(
        thinking_levels=("off", "minimal", "low", "medium", "high", "xhigh"),
        thinking_models=("gpt-5.5",),
        thinking_parameter="reasoning.effort",
    )

    off_provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="gpt-5.5",
        thinking_level="off",
    )
    minimal_provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="gpt-5.5",
        thinking_level="minimal",
    )
    xhigh_provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="gpt-5.5",
        thinking_level="xhigh",
    )

    assert isinstance(off_provider, OpenAICodexProvider)
    assert isinstance(minimal_provider, OpenAICodexProvider)
    assert isinstance(xhigh_provider, OpenAICodexProvider)
    assert off_provider._config.reasoning_effort is None
    assert minimal_provider._config.reasoning_effort == "low"
    assert xhigh_provider._config.reasoning_effort == "xhigh"


def test_create_model_provider_coerces_unsupported_startup_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # Regression: startup used to pass the global default ("medium") straight
    # to create_model_provider, which crashed for models like kimi-code:k3
    # that only support xhigh. Now k3 also supports low and high.
    monkeypatch.setenv("TAU_TEST_KIMI_CODE_API_KEY", "test-key")
    store = FileCredentialStore(tmp_path / "credentials.json")
    provider_config = OpenAICompatibleProviderConfig(
        name="kimi-code",
        api_key_env="TAU_TEST_KIMI_CODE_API_KEY",
        models=("k3",),
        default_model="k3",
        thinking_levels=("low", "medium", "high", "xhigh"),
        thinking_default="xhigh",
        thinking_parameter="reasoning_effort",
        model_metadata={
            "k3": ProviderModelMetadata(
                reasoning=True,
                thinking_level_map={
                    "off": None,
                    "minimal": None,
                    "low": "low",
                    "medium": None,
                    "high": "high",
                    "xhigh": "max",
                },
            ),
        },
    )

    with pytest.raises(
        ProviderConfigError,
        match="Thinking mode medium is not available for kimi-code:k3",
    ):
        create_model_provider(
            provider_config,
            credential_store=store,
            model="k3",
            thinking_level="medium",
        )

    provider = create_model_provider(
        provider_config,
        credential_store=store,
        model="k3",
        thinking_level=resolve_startup_thinking_level(provider_config, "k3"),
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._config.reasoning_effort == "max"


@pytest.mark.anyio
async def test_openai_codex_credential_resolver_refreshes_expired_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "openai-codex",
        OAuthCredential(
            access="old-access",
            refresh="old-refresh",
            expires=1,
            account_id="old-account",
        ),
    )

    async def fake_refresh(refresh_token: str) -> OAuthCredential:
        assert refresh_token == "old-refresh"
        return OAuthCredential(
            access="new-access",
            refresh="new-refresh",
            expires=9999999999999,
            account_id="new-account",
        )

    monkeypatch.setattr(provider_runtime, "refresh_openai_codex_token", fake_refresh)

    resolver = OpenAICodexCredentialResolver(
        OpenAICodexProviderConfig(),
        credential_store=store,
    )

    credentials = await resolver()

    assert credentials.access_token == "new-access"
    assert credentials.account_id == "new-account"
    assert store.get_oauth("openai-codex") == OAuthCredential(
        access="new-access",
        refresh="new-refresh",
        expires=9999999999999,
        account_id="new-account",
    )


def test_cross_process_refresh_spends_rotating_token_once(tmp_path) -> None:
    """Two OS processes sharing one store must spend a rotated token once.

    The in-process ``asyncio.Lock`` serializes tasks inside one process only:
    two processes reading the same expired credential both spend the same
    refresh token and one gets ``refresh_token_reused``. Both refreshes run
    in separate OS processes (subprocesses), synchronized by a ready
    rendezvous so both read the stale credential before either writes. The
    network refresh is emulated as a rotating server: the first use of a
    refresh token succeeds and records itself, any later use is rejected.
    """
    store_path = tmp_path / "credentials.json"
    FileCredentialStore(store_path).set_oauth(
        "openai-codex",
        OAuthCredential(
            access="old-access",
            refresh="refresh-1",
            expires=1,
            account_id="old-account",
        ),
    )
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    refresh_log = tmp_path / "refreshes.log"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CROSS_PROCESS_REFRESH_SCRIPT,
                str(store_path),
                str(ready_dir),
                str(refresh_log),
                str(tmp_path / f"result-{index}"),
                index,
            ],
            env=env,
        )
        for index in ("0", "1")
    ]
    try:
        for process in processes:
            assert process.wait(timeout=60) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    results = {index: (tmp_path / f"result-{index}").read_text() for index in ("0", "1")}
    assert results == {"0": "access-2\nacct-2", "1": "access-2\nacct-2"}
    assert refresh_log.read_text() == "refresh-1\n"
    assert Path(f"{store_path}.lock").exists()


def test_same_process_concurrent_refresh_on_one_store_does_not_deadlock(
    tmp_path,
) -> None:
    """Two expired credential names on one store refresh concurrently.

    Two tasks refresh different credential names, so both pass the per-name
    locks. The first task holds the file lock across its network refresh. A
    second task without a per-path gate blocks the loop thread inside
    ``flock`` while the file-lock owner waits on that same loop: the process
    hangs. The per-path gate serializes both tasks before the file lock, so
    ``flock`` only waits on other processes.

    The pre-fix hang freezes the loop thread, so an ``asyncio.timeout`` in
    the scenario never fires: the scenario runs in a subprocess whose hard
    wait timeout turns the permanent hang into a fast test failure.
    """
    store_path = tmp_path / "credentials.json"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SAME_STORE_CONCURRENT_REFRESH_SCRIPT,
            str(store_path),
            str(tmp_path / "result.txt"),
        ],
        env=env,
    )
    try:
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert (tmp_path / "result.txt").read_text() == "new-refresh-a\nnew-refresh-b"
    store = FileCredentialStore(store_path)
    assert store.get_oauth("codex-a") == OAuthCredential(
        access="new-refresh-a",
        refresh="fresh-refresh-a",
        expires=9999999999999,
        account_id="acct-refresh-a",
    )
    assert store.get_oauth("codex-b") == OAuthCredential(
        access="new-refresh-b",
        refresh="fresh-refresh-b",
        expires=9999999999999,
        account_id="acct-refresh-b",
    )


@pytest.mark.anyio
async def test_codex_resolver_holds_the_file_lock_during_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The file lock wraps the codex re-read, network refresh, and write."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "openai-codex",
        OAuthCredential(
            access="old-access",
            refresh="old-refresh",
            expires=1,
            account_id="old-account",
        ),
    )

    async def fake_refresh(refresh_token: str) -> OAuthCredential:
        assert refresh_token == "old-refresh"
        # The sibling lock file exists while the network refresh runs; an
        # unlocked implementation never creates it.
        assert Path(f"{store.path}.lock").exists()
        return OAuthCredential(
            access="new-access",
            refresh="new-refresh",
            expires=9999999999999,
            account_id="new-account",
        )

    monkeypatch.setattr(provider_runtime, "refresh_openai_codex_token", fake_refresh)

    resolver = OpenAICodexCredentialResolver(
        OpenAICodexProviderConfig(),
        credential_store=store,
    )

    credentials = await resolver()

    assert credentials.access_token == "new-access"
    assert Path(f"{store.path}.lock").exists()


@pytest.mark.anyio
async def test_runtime_oauth_resolver_holds_the_file_lock_during_refresh(
    tmp_path,
) -> None:
    """The file lock wraps the OAuth runtime re-read, refresh, and write."""
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "anthropic",
        OAuthCredential(access="old", refresh="refresh-token", expires=1),
    )

    class FakeOAuthProvider:
        id = "anthropic"
        name = "Fake Anthropic"
        flow_kinds = ("device_code",)

        async def login(self, _callbacks: OAuthLoginCallbacks) -> OAuthCredential:
            raise AssertionError("not used")

        async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
            # The sibling lock file exists while the network refresh runs; an
            # unlocked implementation never creates it.
            assert Path(f"{store.path}.lock").exists()
            return OAuthCredential(
                access="new-access",
                refresh="new-refresh",
                expires=9999999999999,
            )

        def runtime_auth(self, credential: OAuthCredential) -> OAuthRuntimeAuth:
            return OAuthRuntimeAuth(api_key=credential.access)

    provider = provider_config_from_catalog_entry("anthropic")
    register_oauth_provider(cast(OAuthProvider, FakeOAuthProvider()))
    try:
        auth = await OAuthRuntimeCredentialResolver(provider, credential_store=store)()
    finally:
        unregister_oauth_provider("anthropic")
        reset_oauth_providers()

    assert auth.api_key == "new-access"
    assert Path(f"{store.path}.lock").exists()


def test_file_refresh_lock_is_a_persistent_sibling_of_the_store(tmp_path) -> None:
    """The cross-process lock lives at ``<store_path>.lock`` and releases on exit."""
    store_path = tmp_path / "credentials.json"
    lock_path = Path(f"{store_path}.lock")

    with _file_refresh_lock(store_path):
        assert lock_path.exists()

    # The lock file persists after the refresh: deleting it after unlock
    # reopens the race between the unlock and the next process's open.
    assert lock_path.exists()
    # A second acquisition blocks indefinitely if the first exit leaked the
    # lock, so re-acquiring proves the lock was released.
    with _file_refresh_lock(store_path):
        pass


@pytest.mark.anyio
async def test_oauth_runtime_refresh_fails_when_the_file_lock_is_unavailable(
    tmp_path,
) -> None:
    """A failed lock acquisition fails the refresh; it never proceeds unlocked.

    An unlocked refresh risks spending a rotated token twice, so on platforms
    with ``flock``/``msvcrt`` an ``OSError`` opening the lock file surfaces as
    a refresh error instead of a silent unlocked run.
    """
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "anthropic",
        OAuthCredential(access="old", refresh="refresh-token", expires=1),
    )
    # A directory at the lock path makes open() raise OSError.
    Path(f"{store.path}.lock").mkdir()

    class FakeOAuthProvider:
        id = "anthropic"
        name = "Fake Anthropic"
        flow_kinds = ("device_code",)

        async def login(self, _callbacks: OAuthLoginCallbacks) -> OAuthCredential:
            raise AssertionError("not used")

        async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
            raise AssertionError("refresh must not run unlocked")

        def runtime_auth(self, credential: OAuthCredential) -> OAuthRuntimeAuth:
            raise AssertionError("not used")

    provider = provider_config_from_catalog_entry("anthropic")
    register_oauth_provider(cast(OAuthProvider, FakeOAuthProvider()))
    resolver = OAuthRuntimeCredentialResolver(provider, credential_store=store)
    try:
        with pytest.raises(OSError):
            await resolver()
    finally:
        unregister_oauth_provider("anthropic")
        reset_oauth_providers()
