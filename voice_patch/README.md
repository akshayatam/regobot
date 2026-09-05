# Voice patch — re-apply after every regodit folder update

The ElevenLabs integration lives inside `regodit/src/regodit/ui/app.py`.
**Replacing the regodit folder wipes it.** Run this immediately after:

```bash
python3 voice_patch/apply_voice_patch.py
```

Works from either layout: `voice_patch/` sitting next to the checkout
(`HackathonNYC/regodit`) or inside it (`regobot-main/voice_patch`). It prints which
`app.py` it targeted. Override the agent with `ELEVENLABS_AGENT_ID=... python3 ...`.

Then restart the server:

```bash
cd regodit
PYTHONPATH=src python3 -m regodit serve --port 8501
```

Safe to run twice — it detects what's already there and skips it.

## What it adds

| | |
|---|---|
| `AppService.match_question` | maps free-text speech → questionnaire item (stemming + security-term aliases) |
| `AppService.voice_ask` | runs the same investigation the web UI runs, returns a `spoken_answer` |
| `AppService.voice_record` | records what the user says back, via the normal follow-up path |
| `POST /api/voice-ask` | the ElevenLabs server tool endpoint |
| `POST /api/voice-record` | for recording spoken answers |
| CORS + `OPTIONS` | so ElevenLabs' cloud can reach it |
| `<elevenlabs-convai>` | the widget on the page, with CSS that keeps it off the Send button |

`voice_record` also fans the spoken confirmation out through `AppService.synchronizer`,
so one spoken answer updates every questionnaire row mapped to that control - the same
guarantee the typed chat channel has. Without it the voice channel would silently update
one row while chat updated all of them.

## If it fails

It prints which anchor it couldn't find and stops without writing anything.
That means upstream rewrote that part of `app.py`. Compare against
`app.patched.reference.py` in this folder and merge the voice sections by hand.

`regodit/src/regodit/ui/app.py.pre-voice` is the unpatched baseline.

## The rule the patch protects

The voice agent must never answer from its own knowledge. It calls the tool,
reads `spoken_answer` verbatim, and says UNKNOWN when the evidence doesn't
support an answer. Same guarantee as the text UI, same golden rule.

## Exposing it to ElevenLabs (Cloudflare)

ElevenLabs calls the tool from its cloud, so the server needs a public URL:

```bash
cloudflared tunnel --url http://127.0.0.1:8501
```

Take the printed `https://<name>.trycloudflare.com` and set it as the agent's server-tool
base URL in the ElevenLabs dashboard:

| Tool | Method | URL |
|---|---|---|
| `voice_ask` | POST | `https://<name>.trycloudflare.com/api/voice-ask` |
| `voice_record` | POST | `https://<name>.trycloudflare.com/api/voice-record` |

`voice_ask` body: `{"question": "<what the caller said>"}`
`voice_record` body: `{"question_id": "<from voice_ask>", "response": "<what the caller said>"}`

The agent must read `spoken_answer` back verbatim and must not answer from its own
knowledge. Both endpoints already send `Access-Control-Allow-Origin: *` and answer the
`OPTIONS` preflight.

**Quick tunnels are ephemeral.** The hostname changes every time `cloudflared` restarts,
so re-paste the URL into the ElevenLabs tool config after each restart.

## Tests

`tests/test_voice.py` covers matching, the refusal path, evidence-cited answers, recording,
multi-row fan-out, and the CORS/route surface. It skips itself on an unpatched checkout.
