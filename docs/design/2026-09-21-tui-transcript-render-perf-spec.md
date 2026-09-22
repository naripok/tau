# Spec: TUI transcript render performance

## Domain: TUI transcript rendering

The transcript is the scrollable region that shows one row per piece of
conversation content. This spec formalizes the complete post-change behavior of
the transcript render pipeline. The baseline is an undocumented existing domain,
so every requirement below is ADDED to the absent living spec. The requirements
cover the established unchanged behavior and the changes that remove redundant
remounts, bound the stream flush cadence, and cache each markdown parser.

### Definitions

- **Display state**: the mutable render input that the app owns. It holds the
  item list, the streaming assistant buffer, and the visibility flags for tool
  results and thinking.
- **Display item**: one entry of the display-state item list.
- **Row**: one mounted transcript widget. The message row renders one display
  item. The placeholder row stands in for a run of hidden thinking items. The
  streaming block receives live assistant or thinking deltas. The boundary
  marker shows the count of items outside the window. The retry notice appears
  while a failed turn waits to retry.
- **Widget object identity**: two references to the same mounted widget
  instance. Identity assertions compare widget objects, not widget contents.
- **Transcript window**: the bounded slice of display items whose rows are
  mounted. A full rebuild or a page shift clamps the window to 200 display
  items. Appends grow the mounted window up to 240 window items before an
  append compaction rebuild clamps it.
- **Latest window**: a window that extends through the last display item, so no
  later items are hidden.
- **Render fingerprint**: the set of render inputs that a row records. Two
  fingerprints are equal when every input is equal.
- **Render inputs**: the item content and role, the theme, the tool-result
  visibility flag, the custom markup, the resolved tool invocation, and the
  resolved tool result markup. The resolved invocation of a running tool
  includes its elapsed-time suffix.
- **Refresh**: the app-level update that refreshes the surrounding chrome and
  renders display state into the transcript.
- **Diff render**: a transcript render that reuses mounted rows whose
  fingerprints match the desired render inputs. A mounted row is reusable at a
  position only when the desired row entry at that position resolves to that
  exact row object (the item's bookkeeping row, still a child of the transcript,
  with a fingerprint equal to the item's current render fingerprint), or when
  the desired row and the mounted row have both the same row kind and an equal
  fingerprint. A streaming block is resolvable only for the display item it is
  already mapped to. When the active streaming block is mounted, the desired
  assistant entry resolves to that exact row object. Every other mismatched
  position mounts a newly constructed row, and unmatched mounted rows are
  removed after mounting. It touches only mismatched positions.
- **Full rebuild**: a transcript render that removes every mounted row and
  mounts a fresh row for every window entry.
- **Route precedence**: the rule that resolves overlap between the full rebuild
  and the keep or zero-remount guarantees. The keep and zero-remount guarantees
  govern a refresh that routes to the diff render. When a refresh routes to the
  full rebuild, the full rebuild governs: it replaces the mounted streaming
  block with one constructed from the assistant buffer, registers that block in
  the active-stream bookkeeping, and loses no content.
- **Remount**: removal of a mounted row together with a mount of a fresh row for
  the same content.
- **Fast path**: a diff render outcome that performs no mounts, no removals, and
  no layout refresh.
- **Batched update**: the UI framework mechanism that groups DOM changes so that
  layout and repaint run once for the group.
- **Active-stream bookkeeping**: the tracking that the transcript keeps for the
  active assistant block, the active thinking block, and the active message rows.
- **Stream flush**: one coalesced write of the pending streamed fragments of a
  streaming block to its markdown renderer.
- **In-place update route**: the update method that re-renders the content of a
  mounted row without remounting it.
- **Baseline rendering**: the visible transcript output that the pipeline
  produced before this change.

### ADDED Requirements

#### Requirement: Render fingerprint covers render inputs

Every item-backed row (the message row, the placeholder row, the streaming
block, and the notice row) SHALL record a render fingerprint at construction. A
boundary marker records no render fingerprint; the boundary-marker requirement
governs its reuse. A row SHALL recompute its fingerprint at exactly these
points: construction, an
in-place row update, the completion of streaming finalization, and the swap of
the displayed item on a streamed block. The fingerprint SHALL cover these
render inputs:

- the item content and role
- the theme
- the tool-result visibility flag
- the custom markup
- the resolved tool invocation
- the resolved tool result markup

The diff render SHALL re-resolve the invocation of a running tool on every
refresh. The elapsed-time suffix of that invocation takes part in the
fingerprint comparison.

##### Scenario: A changed render input forces row replacement

- GIVEN a settled transcript with no running tool timer
- WHEN a refresh follows a change to exactly one render input of one window item
  (item content, item role, tool-result visibility flag, custom markup,
  resolved tool invocation, or resolved tool result markup)
- THEN that item position mounts one replacement row and removes exactly one
  row, and every other row keeps its widget object

##### Scenario: An in-place update keeps the fingerprint current

- GIVEN a mounted tool row that received a live progress update through the
  in-place update route
- WHEN a later refresh runs and no render input changed after that update
- THEN the updated row keeps its widget object

##### Scenario: An elapsed suffix advance remounts the timer row

- GIVEN a tool item is running and its resolved invocation carries an
  elapsed-time suffix
- WHEN the suffix advances between two refreshes
- THEN the timer row remounts and every other row keeps its widget object

#### Requirement: Refresh route selection conditions

The system SHALL render a refresh through the diff render when all these
conditions hold:

- the display-state object is the same object as at the previous render
- at least one display item still has its row from the previous render
- the theme is unchanged

The system SHALL render a refresh through the full rebuild when a new
display-state object arrives. The system SHALL also render the full rebuild when
no display item still has its row or when the theme changed. A refresh that
renders a new display-state object or finds no retained rows SHALL re-enable
follow mode before the render.

As a compatibility constraint, the public update methods of the transcript SHALL
keep signatures and behavior compatible with existing callers and tests.

##### Scenario: Same state object with retained rows uses the diff render

- GIVEN the previous render retained rows for display items of the current
  display-state object
- WHEN a refresh runs with the same display-state object and an unchanged theme
- THEN the transcript renders through the diff render

##### Scenario: A new state object or no retained rows uses the full rebuild

- GIVEN a refresh that passes a new display-state object, or a same-state
  refresh where no display item still has its row
- WHEN the refresh runs
- THEN the transcript renders through the full rebuild and re-enables follow
  mode before the render

#### Requirement: Unchanged state refresh remounts nothing

A refresh over unchanged display state SHALL remount no transcript row.
Unchanged display state means that every display item produces the same render
fingerprint across the two refreshes. A refresh that routes to the full rebuild
through the new-display-state-object trigger follows the route-precedence rule.
Identical widget objects in identical
order and unchanged boundary counts select the fast path. The fast path performs
no mounts, no removals, and no layout refresh. On every diff render, a displayed
retry notice stays outside the desired row sequence, so the diff render SHALL
remove it. The refresh SHALL remove the notice and SHALL
keep every other widget object.

##### Scenario: Two refreshes with unchanged state perform zero mounts

- GIVEN a settled transcript with no running tool timer and no displayed retry
  notice
- WHEN two consecutive refreshes run over unchanged display state
- THEN both refreshes mount zero rows and remove zero rows, every mounted widget
  object is identical across the two refreshes, and neither refresh runs a
  layout refresh

##### Scenario: A displayed retry notice is the only removal

- GIVEN a settled transcript with a displayed retry notice and otherwise
  unchanged display state
- WHEN two consecutive refreshes run
- THEN the two refreshes remove exactly one row (the notice), mount zero rows,
  and keep every other widget object identical

#### Requirement: Small changes touch only changed rows

A refresh after small state changes SHALL mount and remove only rows for changed
items, for running tool timer rows whose elapsed suffix advanced, and for a
displayed retry notice, which stays outside the desired row sequence on every
diff render. In-place
item changes and tail appends or removals SHALL touch exactly the changed rows.
A positional shift SHALL remount the shifted suffix of the window. A changed
position SHALL mount a newly constructed row that renders the item exactly as
the full rebuild renders it. The diff render SHALL mount new rows before it
removes unmatched rows, so no empty frame appears. The diff render SHALL request
follow scroll under the same condition as the full rebuild.

##### Scenario: One changed tool result mounts exactly one replacement

- GIVEN a settled transcript with no running tool timer
- WHEN one tool item result changes and a refresh runs
- THEN the refresh mounts exactly one replacement row, removes exactly one row,
  and every other widget keeps its identity

##### Scenario: A tail append mounts only the new row

- GIVEN a settled transcript at the latest window while the viewport follows the
  newest content and the mounted window sits below the append-compaction
  threshold
- WHEN a new display item is appended at the tail and a refresh runs
- THEN exactly the new item row mounts and no other widget changes

##### Scenario: A tail removal removes only that row

- GIVEN a settled transcript at the latest window
- WHEN the last display item is removed from display state and a refresh runs
- THEN exactly that item row is removed and no other widget changes

##### Scenario: A positional shift remounts the shifted suffix

- GIVEN a settled transcript whose window holds several items
- WHEN a display item before the tail is removed and a refresh runs
- THEN the rows at and after the shift position remount and the rows before the
  shift keep their widget objects

##### Scenario: A following viewport stays pinned

- GIVEN the viewport is at the bottom of the transcript
- WHEN a diff render runs
- THEN the view keeps the newest content visible

##### Scenario: A scrolled-up viewport does not jump

- GIVEN the user scrolled up and follow mode is off
- WHEN a diff render runs
- THEN the view does not jump to the bottom

#### Requirement: Boundary markers reuse with count updates

The diff render SHALL reuse a mounted boundary marker instead of remounting it.
It SHALL update the hidden-item count of the marker in place.

##### Scenario: A count update keeps the marker widget

- GIVEN a transcript whose window hides later items, so a bottom boundary marker
  is mounted, while the viewport does not follow
- WHEN a display item is appended at the tail and a refresh runs
- THEN the bottom boundary marker keeps its widget object and shows the updated
  hidden-item count

#### Requirement: Hidden thinking rows collapse to one

Consecutive hidden thinking items SHALL render as exactly one placeholder row
per run. The row bookkeeping SHALL map every item of the run to that shared row.
A placeholder row SHALL fingerprint stably across refreshes. After every render
pass, the system SHALL recompute whether a placeholder row is displayed. The
system SHALL apply the same rule that the full rebuild applies, including the
case where the render kept the existing placeholder row.

##### Scenario: A run of hidden thinking items shows one placeholder row

- GIVEN thinking is hidden and the window holds three consecutive thinking items
- WHEN a refresh runs
- THEN the transcript shows exactly one placeholder row for the run and the row
  bookkeeping maps all three items to that shared row

##### Scenario: A kept placeholder row survives unchanged refreshes

- GIVEN a transcript that displays a placeholder row for a hidden thinking run
- WHEN a second refresh runs over unchanged display state
- THEN the placeholder row keeps its widget object

##### Scenario: The placeholder flag matches the rebuild rule after a refresh

- GIVEN a latest window that displays a placeholder row as its newest content
- WHEN a diff render runs and another hidden thinking delta arrives
- THEN no second placeholder row mounts

#### Requirement: Assistant stream survives a refresh

While the transcript displays the streaming block (the window is latest and the
assistant buffer is non-empty), a refresh during an active assistant stream
SHALL keep the same streaming block widget object mounted, with no remount and
no content loss. A refresh that routes to the full rebuild follows the
route-precedence rule. When the assistant buffer is non-empty and no active
streaming block is mounted, the refresh SHALL construct the block from the
buffer. The refresh SHALL register that block in the same active-stream
bookkeeping that a full rebuild populates. The bookkeeping entries are the
active assistant block
and the active message rows. While the window is latest and the assistant
buffer is non-empty, the transcript SHALL display the streaming block and no
bottom boundary marker.

##### Scenario: The streamed block keeps its object across a refresh

- GIVEN an active assistant stream with a non-empty assistant buffer and a
  mounted streaming block at the latest window
- WHEN a refresh runs
- THEN the same streaming block widget object stays mounted, zero rows mount,
  zero rows are removed for that block, and its accumulated text is unchanged

##### Scenario: Finalization shows the full canonical text

- GIVEN the streamed block that survived a refresh
- WHEN the assistant message finalizes with the canonical text
- THEN the same widget object displays the full canonical text

##### Scenario: A refresh constructs the block from the buffer

- GIVEN a non-empty assistant buffer, no mounted active streaming block, and a
  latest window
- WHEN a refresh runs
- THEN the refresh constructs the streaming block from the buffer, registers it
  as the active assistant block and as an active message row, and the next
  assistant delta appends to that block

##### Scenario: No bottom marker while the stream shows at the latest window

- GIVEN an active assistant stream at the latest window
- WHEN the transcript renders
- THEN no bottom boundary marker is mounted

#### Requirement: Thinking stream refresh keeps baseline

A refresh during an active thinking stream SHALL keep the established visible
behavior. The first refresh that removes the streaming block and its
active-stream bookkeeping SHALL re-render the thinking text from display state
into a new row. Later refreshes with unchanged fingerprints SHALL reuse that
row. The next thinking delta SHALL mount a new streaming block. The established
visible defect that earlier thinking text can remain visible beside the new
streaming block SHALL stay unchanged.

##### Scenario: The resolving refresh re-renders thinking from state

- GIVEN an active thinking stream with thinking shown
- WHEN the first refresh that removes the streaming block and its bookkeeping
  runs
- THEN the streaming block is removed, its bookkeeping is cleared, and the
  thinking text accumulated so far renders from display state into a new row

##### Scenario: Later refreshes reuse the re-rendered row

- GIVEN the re-rendered thinking row
- WHEN a second refresh runs with unchanged fingerprints
- THEN the re-rendered row keeps its widget object

##### Scenario: The next delta opens a new streaming block

- GIVEN the re-rendered thinking row and the cleared bookkeeping
- WHEN the next thinking delta arrives
- THEN a new streaming block mounts and receives later deltas, and the result
  matches the baseline behavior, including the possible duplicate display of
  earlier thinking text

#### Requirement: Finalized stream rows keep their widget

A streamed block SHALL recompute its fingerprint when finalization completes and
when its displayed item is swapped. A refresh with unchanged fingerprints after
finalization SHALL keep that widget object instead of replacing it with a fresh
row. A refresh that routes to the full rebuild through the
new-display-state-object trigger follows the route-precedence rule.

##### Scenario: A finalized message keeps its widget across refreshes

- GIVEN an assistant message finalized onto its streaming block and mapped to
  its canonical display item
- WHEN a refresh runs over unchanged display state
- THEN the finalized widget keeps its widget object and displays the canonical
  text

#### Requirement: Removed stream blocks clear bookkeeping

A diff render that removes a streaming block SHALL clear the active-stream
bookkeeping of that block in the same way the full rebuild clears it. After the
removal, later deltas SHALL open a new streaming block instead of writing to a
removed block.

##### Scenario: An emptied buffer drops the block and its bookkeeping

- GIVEN an active assistant stream with a mounted streaming block
- WHEN the assistant buffer empties and a refresh runs
- THEN the streaming block is removed, the active-stream bookkeeping no longer
  references it, and the next assistant delta opens a new streaming block

#### Requirement: Theme change rebuilds the window

The theme is a render input of the message, placeholder, streaming, and notice
rows. A theme change SHALL remount the window through the full rebuild. The
rebuilt window SHALL show the same content in the new theme.

##### Scenario: A theme change remounts every row

- GIVEN a settled transcript
- WHEN the theme changes and a refresh runs
- THEN the full rebuild remounts the window rows and the visible text is
  unchanged

#### Requirement: Session switch rebuilds in a batched update

A session switch SHALL keep the full-rebuild behavior inside one batched update.
A switch clears and reloads the same display-state object, so the switch SHALL
take the full rebuild through the no-retained-rows condition. After the switch,
the transcript SHALL follow the newest content.

##### Scenario: Resuming a session rebuilds fully in one batched update

- GIVEN a session with mounted transcript rows
- WHEN the user resumes another session
- THEN the transcript rebuilds fully inside one batched update, shows the new
  session items, and follows the newest content

#### Requirement: Full rebuilds run in a batched update

Every full-rebuild site SHALL run inside one batched update. The display-state
render SHALL check that the transcript is mounted before the rebuild. The render
SHALL complete without raising when the transcript is not mounted.

##### Scenario: A full rebuild batches its DOM changes

- GIVEN a mounted app
- WHEN a full rebuild runs from any full-rebuild site (the display-state render,
  a window page shift, an append compaction, a structured finalization that
  overshoots the mounted window, or an append that jumps to the latest window)
- THEN the removals and mounts of that rebuild run inside one batched update

##### Scenario: The display-state render is safe before mount

- GIVEN a transcript that is not mounted
- WHEN the display-state render runs
- THEN it completes without raising

#### Requirement: Window paging keeps current behavior

Window paging SHALL keep its current behavior. Scrolling near the top or bottom
edge SHALL page the window by the established page size (80 items). The page
shift SHALL run as a full rebuild that preserves the window. The paging scroll
SHALL restore the previously visible anchor row. A full rebuild or a page shift
SHALL clamp the window to 200 display items. Appends grow the mounted window up
to 240 window items before an append compaction rebuild clamps it.

##### Scenario: Scrolling to an edge pages the window

- GIVEN a transcript with more display items than the window holds
- WHEN the user scrolls to the top edge
- THEN the window pages earlier and the anchor row the user saw stays in view

#### Requirement: Thinking visibility toggle keeps behavior

The thinking-visibility toggle SHALL keep its current remount path. Toggling
thinking on SHALL render thinking rows from display state. Toggling thinking off
SHALL collapse consecutive thinking rows into one placeholder row per run.
Non-thinking rows SHALL keep their widget objects, and the viewport SHALL keep
its position.

##### Scenario: Hiding thinking replaces only thinking rows

- GIVEN thinking is shown and the window holds thinking rows and tool rows
- WHEN the user hides thinking
- THEN the thinking rows are replaced by one placeholder row per run, every tool
  row keeps its widget object, and the viewport position is preserved

#### Requirement: Tool result toggle keeps in-place updates

The tool-result visibility toggle SHALL keep updating only the mounted rows it
updates today (tool, skill, branch-summary, and compaction-summary rows). Rows
in that set that support the in-place update route SHALL update in place, and
other rows in that set SHALL be replaced.

##### Scenario: Toggling tool results touches only dependent rows

- GIVEN a settled transcript with tool rows and user rows
- WHEN the user toggles tool-result visibility
- THEN only tool, skill, branch-summary, and compaction-summary rows update,
  user rows keep their widget objects, and a later refresh with no further
  changes remounts nothing

#### Requirement: Streaming events keep incremental routes

During a streaming turn, the system SHALL apply session events through the
incremental update routes. Text and thinking deltas
SHALL stream into the mounted streaming blocks. Tool start, update, and end
events SHALL update their rows through the in-place update route. The fallback
for a row that is not mounted or does not support the in-place update is row
replacement or append. The periodic elapsed-time refresh of a running tool row
SHALL keep the in-place update route. At the established terminal boundaries,
the system SHALL render the transcript from display state, and the
route-selection conditions SHALL decide between the diff render and the full
rebuild for that render. The established terminal boundaries are:

- a terminal error or aborted assistant message
- an unexpected run error
- a compaction overflow that aborted or failed

##### Scenario: Deltas stream without a rebuild

- GIVEN a streaming turn with mounted transcript rows
- WHEN text deltas, thinking deltas, and tool progress updates arrive
- THEN no full rebuild runs, the deltas appear in the streaming blocks, and tool
  events touch only their own rows

##### Scenario: Terminal error boundaries render once

- GIVEN a streaming turn
- WHEN a terminal error assistant message ends the turn
- THEN one transcript render from display state runs at the boundary, the error
  row is visible after that render, and a full rebuild runs only when the
  route-selection conditions force one

#### Requirement: Stream flush cadence is fixed at 0.05s

The stream flush interval SHALL be a fixed 0.05-second value defined as a
single named constant that tests can replace. A streaming block SHALL flush at
most once per interval, and every pending fragment SHALL reach the markdown
renderer in order. The staleness bound of the visible stream SHALL be the
constant itself.

##### Scenario: The cadence follows a replaced constant

- GIVEN the flush interval constant is replaced with a test value
- WHEN fragments arrive across flush windows
- THEN the flush schedule follows the replaced value and the concatenated writes
  equal the canonical streamed text in order

##### Scenario: The default constant is 0.05 seconds

- GIVEN no replacement of the constant
- WHEN the flush constant is read
- THEN its value equals 0.05 seconds

#### Requirement: Parser built once per markdown widget

Each markdown rendering row SHALL construct its markdown parser at most once in
its lifetime. Streamed appends and document updates SHALL reuse the constructed
parser instead of constructing a new parser per write.

##### Scenario: Appends and updates reuse one parser

- GIVEN a streaming block constructed with a counting parser factory
- WHEN the block renders its initial document, receives ten streamed appends,
  and receives a full document update
- THEN the factory constructed exactly one parser in the block lifetime

#### Requirement: Visible output matches baseline rendering

The visible transcript output SHALL stay identical to the baseline rendering in
every scenario this spec covers.

##### Scenario: Diff render output equals full rebuild output

- GIVEN a populated transcript with user, assistant, thinking, tool, custom,
  error, and status items
- WHEN a diff render runs
- THEN the mounted rows, their order, and their visible text are identical to a
  full rebuild of the same display state

##### Scenario: Established suites keep passing

- GIVEN the established transcript test suites
- WHEN they run after this change
- THEN they pass unchanged except the two adjusted tests: the flush-window
  test, whose sleeps follow the replaced flush constant, and the streaming
  code-block test, whose wait spans the replaced flush window so the code
  block exists when the test queries it

#### Requirement: Benchmark script records before and after

The change SHALL ship a committed benchmark script that reproduces the rows and
the methodology of the measurement table. The script SHALL print one row per
measured operation:

- the full window rebuild
- the layout refresh of the settled transcript
- the in-place update of one tool row
- one stream flush cycle, as incremental cost over the idle floor
- the parser construction

The methodology SHALL match the measured conditions:

- the pinned framework versions
- a headless app test harness
- a 120 by 40 terminal
- a populated transcript of 300 items with 200 tool rows and 100 markdown
  messages

The performance dev-note SHALL record the before value and the after value for
each measured operation. The responsiveness dev-note SHALL state the replaced
0.05-second flush cadence in its cadence sentence.

##### Scenario: The benchmark script prints one row per operation

- GIVEN the repository
- WHEN the benchmark script runs
- THEN it prints exactly one row for each of the five measured operations

##### Scenario: The dev-notes record the measurements and the cadence

- GIVEN the performance dev-note and the responsiveness dev-note
- WHEN they are inspected after this change
- THEN the performance dev-note records a before value and an after value for
  every measured operation, and the responsiveness dev-note cadence sentence
  states the 0.05-second cadence
