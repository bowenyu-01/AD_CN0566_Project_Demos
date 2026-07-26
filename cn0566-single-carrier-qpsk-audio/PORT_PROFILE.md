# Fixed hardware port profile

```text
Pluto TX2A         -> CN0566 TX_IN
CN0566 TX_OUT_2    -> external Vivaldi transmit antenna
CN0566 RX1         -> Pluto RX1A
CN0566 RX2         -> Pluto RX2A
```

The normal realtime profile enables Pluto TX channel index 1 (TX2A) and Pluto
RX channel index 1 (RX2A). Both receive coax cables may remain connected. Set
`DECODE_CHANNEL=mrc` only after RX2-only operation is stable with zero host
drops.
