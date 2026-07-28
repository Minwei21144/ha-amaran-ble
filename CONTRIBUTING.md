# Contributing

Thank you for helping improve Amaran BLE.

## Before opening an issue

1. Confirm that Sidus Link and Amaran Desktop are closed.
2. Confirm that the Bluetooth adapter or ESPHome Bluetooth Proxy can create
   active GATT connections.
3. Reproduce the problem with debug logging enabled.
4. Download integration diagnostics and review them before attaching them.
5. Remove all Mesh keys, Home Assistant tokens, private keys, local addresses
   and unrelated integration data from any additional log.

Do not post exported Amaran configuration JSON.

## Development setup

Python 3.13 and `cryptography` are used by CI:

```powershell
python -m pip install cryptography
python -m unittest discover -s tests -v
python -m compileall -q custom_components tests tools
```

Keep changes scoped to `custom_components/amaran_ble`, tests and project
documentation. This integration must not require changes to Home Assistant's
native Bluetooth settings, ESPHome Bluetooth Proxy firmware or unrelated
integrations.

## Pull requests

- Add focused regression tests for behavior changes.
- Keep example MAC addresses synthetic and keys obviously non-production.
- Update `CHANGELOG.md` for user-visible changes.
- Describe physical hardware validation separately from offline tests.
- Do not commit generated logs, probe output, archives or credentials.
- Run the full offline test suite before requesting review.

The project targets HACS distribution. Changes must preserve a single
integration under `custom_components/amaran_ble` and pass HACS validation and
Hassfest.
