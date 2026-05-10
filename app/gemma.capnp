@0xb89e7c0d3a4b9f1e;

# Cap'n Proto schema for the bfagent backend <-> frontend RPC.
# The runtime copy lives embedded in schema.py; keep these in sync.

struct Turn {
    role    @0 :Text;
    content @1 :Text;
}

# Streaming callback the client passes into chatStream(). The server
# invokes chunk() for each delta as the model generates and done() exactly
# once when generation finishes (or fails). chunk() callbacks are E-order:
# they're delivered in the order the server made them.
interface ChatSink {
    chunk @0 (text :Text) -> ();
    done  @1 (error :Text) -> ();
}

interface GemmaAgent {
    # Non-streaming: blocks until the full reply is generated, then returns
    # it. Used by the Gradio frontend, which doesn't render token-by-token.
    chat @0 (message    :Text,
             history    :List(Turn),
             imageBytes :Data,
             imageMime  :Text,
             maxTokens  :UInt32 = 400)
       -> (reply :Text, error :Text);

    # Streaming: server pushes deltas to `sink` as they're produced, calls
    # `sink.done(error)` when finished, and only then returns from the
    # call. Errors after the first chunk are reported via sink.done(error).
    chatStream @1 (message    :Text,
                   history    :List(Turn),
                   imageBytes :Data,
                   imageMime  :Text,
                   maxTokens  :UInt32 = 400,
                   sink       :ChatSink)
       -> ();
}
