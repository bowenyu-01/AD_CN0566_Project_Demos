#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"

echo "CN0566/Pluto non-transmitting IIO diagnostic"
echo "Profile: ${OFDM_PROFILE}"
echo

echo "Processes currently mentioning CN0566/OFDM/Pluto:"
pgrep -af 'cn0566_ofdm|cn0566_rf_sanity|iio|Pluto' || true

echo
echo "IIO contexts:"
iio_info -s || true

echo
echo "Direct Pluto sampling-frequency readback:"
SDR_URI="$SDR_URI" python3 - <<'PY'
import os
import adi
uri = os.environ['SDR_URI']
print('Pluto URI:', uri)
sdr = adi.ad9361(uri=uri)
channel = sdr._ctrl.find_channel("voltage0", False)
if channel is None:
    raise SystemExit("FAIL: AD9361 voltage0 control channel not found")
value = channel.attrs["sampling_frequency"].value
print("sampling_frequency raw:", repr(value))
print("sampling_frequency int:", int(float(value)))
print("PASS: scalar IIO readback")
PY

echo
echo "If this reports READ LINE -32, a list instead of one value, or a timeout:"
echo "  1. Stop every program using Pluto."
echo "  2. Disconnect/reconnect or power-cycle Pluto/CN0566."
echo "  3. Wait at least five seconds."
echo "  4. Run this diagnostic again before transmitting."
