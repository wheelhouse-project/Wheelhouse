# Samsung TV setup

WheelHouse can adjust Samsung TV hardware brightness through its existing brightness controls. The verified device is the Samsung MRN65R95HAFXZA (R95H), firmware T-RSMHAKUC-0100-1235.0. This implementation assumes the same API and native 0-50 brightness range across Samsung TVs, as David directed; direct hardware testing was performed on the R95H. The connection uses Samsung's local IP Control API and a TV-approved pairing saved with Windows encryption.

## Pair the TV

Enable **IP Remote** in the TV's network expert settings and note its IP address. Create a private local folder for the pairing file. From the WheelHouse repository root, use the WheelHouse Python environment to run `scripts/pair_samsung_tv.py` with these required arguments:

| Argument | Value |
| --- | --- |
| `--ip-address` | The TV's current local IPv4 address |
| `--credential-file` | An absolute path to a new file in that existing folder |
| `--expected-model` | The TV's exact model identifier; the tested R95H reports `MRN65R95HAFXZA` |

The environment's Python executable is `services/wheelhouse/.venv/Scripts/python.exe`. Approve the pairing prompt on the TV. Pairing reads brightness but does not change picture settings. The saved file can be used by the same Windows account; runtime commands reuse it without another pairing prompt. Re-pairing requires a new destination filename.

## Configure WheelHouse

In `services/wheelhouse/config.toml`, add a `[plugins.samsung]` table with:

| Setting | Value |
| --- | --- |
| `enabled` | `true` |
| `ip_address` | The quoted address used during pairing |
| `credential_file` | The quoted absolute path to the saved pairing file; use forward slashes in the TOML string |

For a Samsung installation, set `enabled = false` in the existing `[plugins.bravia]` table. Do not duplicate an existing TOML table.

The existing shared audio code still requires `device_name` in `[plugins.bravia]`, even when the Sony plugin is disabled. Put the Samsung audio output's Windows device name there. The setting's old name does not require Sony hardware. General audio monitoring uses Windows' default audio output. Choose the audio format, including Dolby Atmos for Home Theater, in Windows sound settings. The current application has no active automatic call to its old spatial-sound helper. An empty `SPATIAL_SOUND_EXEC` disables that helper; it does not switch Windows spatial sound off. Cleanup of the obsolete configuration requirement is tracked separately in P3 bead `wh-9bisk`.

Restart WheelHouse after changing configuration. Samsung hardware access occurs only when `plugins.samsung.enabled` is explicitly `true`. An absent setting leaves the discovered Samsung plugin inactive.

## Operation and verification

Use the existing brightness controls. WheelHouse's 0–100 scale maps to the TV's 0–50 backlight range. Changes are read back from the TV before success is reported. Dimming beyond the hardware minimum uses the existing configured software dimmer. If the TV goes offline after reporting minimum brightness, the existing software dimmer can continue dimming, as with Sony. An unknown or nonminimum last state does not trigger that fallback. Connection and pairing failures are reported; the next hardware command retries without automatically re-pairing or selecting another TV.

Shutdown waits for an in-flight brightness write and its readback to finish. It does not turn off the TV or restore its hardware brightness.

The real R95H passed a small brightness change and restoration with independent readback. David subsequently tested the revised application after restoration of the shared files and the Samsung-only shutdown fixes, and confirmed that screen dimming works. He also confirmed the TV settings panel's minimum of 0 and maximum of 50. The affected automated suite passed 481 tests, including brightness limits, software-dimming handoff, shutdown and recovery behavior.

Physical disconnect/recovery, saved pairing after a TV power cycle, and behavior in additional picture/HDR modes have not been tested on the TV. These are hardware-verification limits, not observed failures. Keep the original picture settings when checking them. To verify Atmos independently, play known Atmos content from the PC and inspect the soundbar's received audio format; the brightness plugin does not configure or certify that audio path.
