# Cache keys

kiln stores each build output under a key derived from the target's manifest, so two machines
that build the same thing agree on where it lives in the shared cache. A key is 16 lowercase
hex digits.

## How a key is computed

1. Load `kiln.toml` as TOML.
2. In the `[target]` table, drop `description` and `owners`. They say what a target is for and
   who looks after it, not what it builds.
3. Sort `target.inputs`. The order inputs are listed in does not matter.
4. Encode the result as canonical JSON (below).
5. Hash `kiln/2`, one NUL byte and the canonical JSON with SHA-256. The key is the first 16 hex
   digits of the digest.

## Canonical JSON

- Object keys are sorted by code point, in every object at every depth.
- No whitespace between tokens: `,` and `:` only.
- Strings are NFC normalized, keys included, and written as UTF-8. Nothing is escaped except
  what JSON requires (`"`, `\` and control characters).
- Numbers with no fractional part are written as integers, so `4.0` and `4` give the same key.
  NaN and infinities are refused.
- Arrays keep their order, except `target.inputs` (see above).

## Format versions

The `kiln/N` prefix changes whenever a change to these rules changes existing keys. Every key
in one shared cache must come from the same format.

- `kiln/1`: first release (0.2.0).
- `kiln/2`: `target.inputs` sorted before hashing (0.3.0).
