# Release provenance

This repository is a GitHub consolidation of the documented
`1.2.0-realtime-audio` project behavior. The original final-development archive
was not present in the active packaging workspace, so the public source tree was
reconciled from the documented source hierarchy, fixed modem constants,
validated runtime behavior and prior project artifacts.

The release therefore does **not** claim byte-for-byte identity with every
intermediate development copy. It does preserve the documented over-the-air
packet format, waveform profile, physical port map, native Viterbi interface,
RX2-default realtime architecture and one-second TX queue design. Acceptance is
based on the included native build, unit tests, exact end-to-end simulation and
performance simulation.
