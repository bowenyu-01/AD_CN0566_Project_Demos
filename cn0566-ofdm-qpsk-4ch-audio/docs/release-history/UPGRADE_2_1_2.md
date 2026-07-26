# Upgrade to 2.1.2 consistent-stable

Use the supplied consistency patch on the existing project folder. It backs up
the current files and complete test directory, removes stale tests and Python
bytecode, installs a unified runtime, rebuilds the native library, and runs all
non-RF acceptance checks.

```bash
cd ~/Desktop
unzip CN0566_OFDM_4CH_RPi4_CONSISTENCY_PATCH_2_1_2.zip
cd CN0566_OFDM_4CH_RPi4_CONSISTENCY_PATCH_2_1_2
chmod +x apply_patch.sh
./apply_patch.sh ~/Desktop/QPSK_test/CN0566_OFDM_4CH_RPi4_Release
```

Do not proceed to RF unless the final line reports that the `stable_1m`
software-only realtime budget checks passed.
