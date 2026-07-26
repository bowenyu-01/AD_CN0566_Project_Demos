# Troubleshooting

## No detected packets

- Run `run_rf_sanity.sh` first.
- Check PLL lock, +187.5 kHz orientation and both coax routes.
- Confirm `TX_OUT_2` selection and Vivaldi polarization.
- Raise TX level gradually only while monitoring ADC headroom.

## Audio breaks up

The application needs roughly 50 valid packets/s. A larger audio buffer cannot
correct an average rate below that value. Inspect `host_drop`, `tx_starve`,
sequence gaps and ALSA underruns. Use RX2-only mode before testing MRC.

## `READ LINE: -32` / broken pipe

Stop all Pluto processes, wait for IIO buffers to close, reconnect or power-cycle
the supported assembly, and verify `iio_info -s` before restarting.

## SciPy cannot load `libopenblas.so.0`

```bash
sudo apt install -y libopenblas0 python3-scipy
```

Create the virtual environment with `--system-site-packages`.

## Numba/llvmlite failure on 32-bit Pi

Numba is not required. The launch scripts set `CN0566_DISABLE_NUMBA=1`; build
`libcn0566_viterbi.so` locally with `./build_native.sh`.
