# OmaYap backend protocol v1

This document defines the private protocol between the OmaYap service and a
speech backend. It is deliberately small so a future OCR or native
backend adapter can replace the Piper worker without changing the service
boundary.

## Transport

The backend is a child process. Requests are UTF-8 JSON objects, one object per
newline, on stdin. Responses are UTF-8 JSON objects, one object per newline, on
stdout. A backend must not write diagnostics to stdout; stderr is not consumed
by the service and must not contain selected text.

Voice selection is intentionally outside this speech protocol. The service
invokes the dependency-free `bin/manage-voices` helper for `list`, `install`,
and `select`; that helper accepts only IDs from the immutable
[`share/voices.json`](../share/voices.json) catalog. It downloads only the
catalog's fixed HTTPS Piper files, checks exact byte counts and SHA-256 values,
and writes only the private `XDG_DATA_HOME/omayap-read-aloud/models/` tree plus
the atomic `selected-voice` state. `list --json` returns bounded metadata only:
IDs, labels, region/quality, approximate size, installation state, and official
links. It never returns model bytes, selected text, arbitrary paths, or
arbitrary URLs. Setup installs and selects only the catalog default; alternate
voices are downloaded only after an explicit popup action.
Selecting an uninstalled catalog entry performs the verified install and then
selects it automatically; no model path or URL comes from the QML caller.

The protocol version is the integer `1`. A request may omit `protocolVersion`
for compatibility with the original worker; omission means v1. If a request
contains another value, the backend emits one metadata-only state event with
`errorCode: "unsupported-protocol-version"` and does not execute the request.

## Adapter boundary

OCR capture is an adapter outside this transport. It hands bounded UTF-8 text
to the same generic `speak` request; it does not add a second backend command
or put text in a state event. The OCR adapter reuses
Omarchy's installed local capture command through a reviewed stdout-only shim,
preserves `OMARCHY_OCR_LANGS`, and does not write CLIPBOARD. It does not store
recognized text in a regular file, command-line argument, environment value,
diagnostic, notification, or state event. A future C++/Rust backend can
implement this same stdin/stdout contract without changing that adapter
boundary.

## Requests

Every request has a `command` string. The supported commands are:

```json
{"protocolVersion":1,"command":"speak","text":"...","requestId":"selection-1","cleanupProfile":"safe"}
{"protocolVersion":1,"command":"read-selection","text":"...","requestId":"selection-1","cleanupProfile":"article"}
{"protocolVersion":1,"command":"status"}
{"protocolVersion":1,"command":"stop"}
{"protocolVersion":1,"command":"set-speed","speed":1.25}
{"protocolVersion":1,"command":"shutdown"}
```

`speak` is the generic command. `read-selection` is an exact compatibility
alias and has identical validation and lifecycle behavior. `text` is required
for both commands. The backend counts and enforces the 20,000 Unicode
code-point limit before applying any cleanup; text that is over the limit is
rejected even if cleanup would remove characters. Empty or whitespace-only
text after the selected profile is applied is rejected with
`errorCode: "empty-selection"`.

`cleanupProfile` is optional and defaults to `safe`. It must be one of
`off`, `safe`, or `article`; any other value receives the fixed
`errorCode: "invalid-cleanup-profile"` and is never echoed. `off` performs
only the existing line-ending and NFC normalization. `safe` additionally
maps Unicode horizontal/paragraph whitespace, removes C0/C1 controls except
meaningful tab/newline whitespace, removes soft hyphen, zero-width space,
word joiner, and BOM, and collapses excessive whitespace. It preserves ZWNJ,
ZWJ, bidi marks/isolates, combining marks, and variation selectors. `article`
adds conservative removal of adjacent MediaWiki-style numeric, note, and
`citation needed` markers; it does not remove arbitrary bracketed prose,
standalone arrays, math subscripts, or code-like markers.

`requestId` is optional. When present it must be a non-empty string of at most
128 Unicode code points. It is opaque to the backend and must not be parsed as
text content. An invalid type, empty string, or oversized value is rejected
with the fixed `errorCode: "invalid-request-id"`; the invalid value is never
echoed. Callers must not put selected text in a request ID.

`speed` is a JSON number. The supported range is 0.5 through 2.0; values
outside that range are clamped by the current Piper backend.

Unknown commands receive the fixed `errorCode: "invalid-command"`.

## State events

Every state event contains the protocol version and fixed metadata fields:

```json
{"event":"state","protocolVersion":1,"status":"speaking","speed":1.0,"characters":42,"cleanupProfile":"safe","requestId":"selection-1"}
```

`status` is one of `setup-required`, `idle`, `capturing`, `loading`,
`speaking`, `stopping`, or `error`. `speed` is a number and `characters` is a
non-negative integer no greater than 20,000. `cleanupProfile` is always one of
the three profile names. `errorCode`, when present, is a short lowercase fixed
token; it is never free-form exception text.

An event for request-scoped work includes that request's `requestId` when the
request supplied one. Events that are not associated with a request omit it.
For example, a successful request normally emits `loading`, `speaking`, and
`idle` with the same ID. A `stop` emits `idle` for the request being stopped,
if one exists. A replacement request receives its own ID and its events are
not attributed to the stopped request.

The optional `audioStarted: true` field is metadata only and marks the first
audio output. It contains no audio or text.
The production PipeWire sink prepends 160 ms of zero-valued PCM to each fresh
`pw-play` stream. This device wake-up guard is not synthesized speech and does
not alter the selected text or backend protocol.

## Lifecycle and cancellation

The backend emits an initial `idle` or `setup-required` snapshot. `speak` and
`read-selection` replace any current request. `stop` cancels the current
request and stops playback promptly; a backend may finish an already-running
native inference before accepting replacement synthesis, but must not run two
requests through one shared model session concurrently. `shutdown` releases
backend resources and ends the process.

The service treats only a matching v1 state event as backend metadata. It
ignores non-JSON and non-state stdout lines. An explicit incompatible worker
version is reported with a fixed local notification and never displayed as raw
backend output.

## Privacy invariants

- Selected text travels only in the request's stdin JSON and transient backend
  memory.
- Text must never appear in stdout, state events, error messages, logs,
  command-line arguments, request IDs, settings, or notifications.
- State events contain counters, fixed statuses, fixed error codes, and opaque
  request IDs only.
- Backends must suppress dependency exception text and must not print model
  input in diagnostics.
- Implementations must enforce the text and request ID limits before allocating
  unbounded work.

## Backend conformance expectations

A future backend adapter is conformant when it:

1. reads and writes newline-delimited JSON without mixing diagnostics into
   stdout;
2. accepts omitted `protocolVersion` as v1 and rejects explicit unsupported
   versions with `unsupported-protocol-version`;
3. implements all six commands and treats `read-selection` as the `speak`
   compatibility alias;
4. validates text, request IDs, and cleanup profiles, returning only fixed
   metadata error codes;
5. emits `protocolVersion: 1` on every state event and preserves a valid
   request ID through that request's lifecycle;
6. keeps selected text out of every response and diagnostic path; and
7. serializes use of a shared model session across cancellation and
   replacement while keeping playback stop responsive.

The repository tests exercise the Piper worker's v1 command, alias, version,
request-ID, lifecycle, and metadata privacy behavior. A replacement backend
should run the same tests or an equivalent protocol-conformance suite before
being selected by the service.
