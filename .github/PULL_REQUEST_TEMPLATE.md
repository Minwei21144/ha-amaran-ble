## Summary

Describe the user-visible behavior and why the change is needed.

## Validation

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m compileall -q custom_components tests tools`
- [ ] `python tools/check_public_tree.py`
- [ ] Physical hardware results are clearly separated from offline tests.
- [ ] No Mesh keys, tokens, private addresses or generated logs are included.

## Hardware

List tested fixture models, Home Assistant version and Bluetooth path, or write
`Not physically tested`.
