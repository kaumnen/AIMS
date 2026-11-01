# Runner notes

Known breakages on the shared pool, newest last. Keep entries short and sign them with your
runner name.

- ci-7: `tests/test_store.py::test_put_replaces_in_place` failed once with "No space left on
  device". /tmp was full on that runner and a rerun passed. Not a kiln bug.
- ci-1: `python -m kiln prune` with the default root prunes this shared cache, other repos'
  outputs included. Pass `--root` with a local directory when you try it.
