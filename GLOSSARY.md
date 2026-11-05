# occupancy-service

A process that publishes each sensor's occupancy from one camera, and the written contract for reading it. Terms this repository uses but does not own are listed at the end, with their owners.

## Language

**Occupancy service**:
A process that takes one archive and one camera, runs the models where the frames are, and publishes L1, and L0 when the detector is installed.

**Live contract**:
The rules in [LIVE.md](LIVE.md).

**Profile**:
A TOML file that says how L1 goes out over MQTT: its topic, its payloads, how often it repeats, and how long it holds a `clear`.

### Owned elsewhere

**Archive**:
`r49`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/r49/blob/main/GLOSSARY.md).

**Sensor**:
`r49`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/r49/blob/main/GLOSSARY.md).

**DPT**:
`r49`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/r49/blob/main/GLOSSARY.md).

**Detector**:
`detector`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/detector/blob/main/GLOSSARY.md).

**L0**:
`detector`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/detector/blob/main/GLOSSARY.md).

**Classifier**:
`occupancy`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md).

**Patch**:
`occupancy`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md).

**L1**:
`occupancy`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md).

**Occupied**:
`occupancy`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md).

**Clear**:
`occupancy`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md).

**Unknown**:
`occupancy`'s word, owned by its [GLOSSARY.md](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md).
