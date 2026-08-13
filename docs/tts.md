# AI read-aloud through a TTS API

Polaris can turn completed AI answers and library daily digests into playable
audio. Polaris doesn't contain or deploy a speech model. It calls a separately
operated OpenAI Speech-compatible API, applies Polaris authentication and
settings at its own boundary, normalizes Markdown, and caches returned WAV
files under `POLARIS_DATA_DIR`.

> **Note:** This is a preview feature currently under active development.

## What users can play

Playback appears only when the administrator enables the provider and the
member enables their personal preference. Polaris never starts speech
automatically.

- PolarisBuddy shows a play or pause control after each completed text answer.
- Literature chat and AI reading chat show the same control after each
  completed assistant answer.
- A library daily digest shows a player for the digest and its rolling trend.

The first click calls the authenticated `POST /api/tts/speech` endpoint. Later
clicks on the same content reuse a content-addressed WAV file. Starting another
player stops the current player.

## Provider API contract

The external service must implement this endpoint:

```http
POST <base-url>/audio/speech
Content-Type: application/json
```

Polaris sends an OpenAI-compatible JSON body:

```json
{
  "model": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
  "input": "Text to read",
  "voice": "default",
  "speed": 1.0,
  "response_format": "wav"
}
```

The service must return a PCM WAV response whose bytes begin with a valid
`RIFF` and `WAVE` header. Polaris rejects responses larger than 128 MiB and
doesn't expose the provider endpoint directly to browsers.

## Configure Polaris

Configure a reachable provider before enabling playback.

1. Deploy the TTS API as a project separate from Polaris.
2. Set the initial values in `.env`:

   ```dotenv
   POLARIS_TTS_ENABLED=true
   POLARIS_TTS_BASE_URL=http://host.docker.internal:50000/v1
   POLARIS_TTS_MODEL=FunAudioLLM/Fun-CosyVoice3-0.5B-2512
   POLARIS_TTS_MAX_CHARS=20000
   ```

3. Recreate the Polaris API container after changing `.env`.
4. Open **Manage > Speech model** as an administrator.
5. Confirm the endpoint, model, default voice, speed, and character limit.
6. Select **Test connection**, enable the service, and then select **Save**.
7. Open **Settings > Speech** as a member and enable playback.

Database-backed administrator settings override the `.env` seed. If you move
the provider after saving settings in the UI, update the endpoint in
**Manage > Speech model** as well.

## Deployment boundary

The speech model and Polaris have separate deployment lifecycles.

- The external TTS project owns CUDA, PyTorch, model files, GPU scheduling,
  reference voices, inference concurrency, and its health endpoint.
- The Polaris API owns user authentication, administrator and member settings,
  request limits, Markdown normalization, upstream error mapping, and the WAV
  cache.
- The React client requests audio only after a user action and manages one
  active browser audio player at a time.

This boundary keeps GPU dependencies, model volumes, and provider-specific
runtime code out of the Polaris repository and images. It also lets operators
replace the model service without rebuilding Polaris, as long as the API
contract stays compatible.

## Security limits

Keep the provider on a trusted network or protect it at the infrastructure
layer. Polaris currently sends no provider API key. The administrator endpoint
setting is trusted configuration, and the API validates its URL shape before
saving it.

> **Warning:** Don't expose voice cloning or user-provided reference audio
> without speaker-consent checks, voice-level authorization, retention rules,
> audit logs, rate limits, and a misuse-response process.

## Troubleshooting

Use the provider health endpoint and Polaris API logs to isolate failures.

- If no play controls appear, enable both the administrator service and the
  member preference, and then refresh the page.
- If **Test connection** fails from Docker, verify that
  `host.docker.internal` resolves in the `api` container and that the external
  service publishes the configured port.
- `TTS_UPSTREAM_UNREACHABLE` means the Polaris API couldn't connect to the
  provider.
- `TTS_UPSTREAM_ERROR:<status>` means the provider returned an error response.
- `TTS_INVALID_AUDIO` means the response wasn't a self-describing WAV file.
- Cached audio is stored in `POLARIS_DATA_DIR/tts-cache`. These files are
  derived artifacts and can be regenerated.

## Next steps

Measure time to first playback, real-time factor, peak VRAM, and failure rate
on representative digest lengths. Add provider authentication and chunked
audio delivery before exposing the speech service across an untrusted network.
