import os
import tempfile
from pathlib import Path

import capnp

# Keep in sync with gemma.capnp at the repo root. The .capnp file is the
# canonical source for humans; this constant is what we ship inside the wheel
# (flat py-modules can't bundle non-py data without extra setup).
SCHEMA_TEXT = """\
@0xb89e7c0d3a4b9f1e;

struct Turn {
    role    @0 :Text;
    content @1 :Text;
}

interface ChatSink {
    chunk   @0 (text :Text) -> ();
    done    @1 (error :Text) -> ();
    approve @2 (payload :Text) -> (decision :Bool);
}

interface GemmaAgent {
    chat @0 (message    :Text,
             history    :List(Turn),
             imageBytes :Data,
             imageMime  :Text,
             maxTokens  :UInt32 = 400)
       -> (reply :Text, error :Text);

    chatStream @1 (message    :Text,
                   history    :List(Turn),
                   imageBytes :Data,
                   imageMime  :Text,
                   maxTokens  :UInt32 = 400,
                   sink       :ChatSink)
       -> ();
}
"""


def _load_schema():
    sibling = Path(__file__).resolve().parent / "gemma.capnp"
    if sibling.is_file():
        return capnp.load(str(sibling))
    fd, path = tempfile.mkstemp(suffix=".capnp", prefix="bfagent_schema_")
    with os.fdopen(fd, "w") as f:
        f.write(SCHEMA_TEXT)
    return capnp.load(path)


_schema = _load_schema()
GemmaAgent = _schema.GemmaAgent
Turn = _schema.Turn
ChatSink = _schema.ChatSink
