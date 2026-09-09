"""
One-time voiceprint enrollment: `python -m voice.enroll`.

Records five short phrases and saves an averaged speaker embedding to
config.ENROLLMENT_PATH. Re-run any time to overwrite it (e.g. after
changing microphones, which shifts the embedding enough to be worth
re-enrolling).

Uses VoiceEncoder.embed_speaker() rather than embedding one long recording:
it averages several independent partial-utterance embeddings, which is the
API Resemblyzer's own docs recommend for a stable per-speaker reference
rather than a single embed_utterance() call.
"""

from resemblyzer import preprocess_wav

from . import audio, config, speaker

_PROMPTS = [
    "Hey, it's me, this is my voice.",
    "The quick brown fox jumps over the lazy dog.",
    "Turn on the desk lights.",
    "What's on my to-do list today?",
    "This is the voice that should be allowed to control things.",
]


def main() -> None:
    print(f"Enrolling your voice at {config.ENROLLMENT_PATH}.")
    print("Read each line aloud after the prompt. ~3 seconds of recording per line.\n")

    wavs = []
    for i, prompt in enumerate(_PROMPTS, 1):
        input(f'[{i}/{len(_PROMPTS)}] Press Enter, then say: "{prompt}"')
        samples = audio.record_seconds(3.5)
        wavs.append(preprocess_wav(samples, source_sr=config.SAMPLE_RATE))
        print("  captured.")

    embedding = speaker._get_encoder().embed_speaker(wavs)
    speaker.save_enrollment(embedding)
    print(f"\nSaved. Test it with: python -m voice.enroll --verify")


def verify() -> None:
    print("Say something (3.5s recording)...")
    samples = audio.record_seconds(3.5)
    matched, score = speaker.is_enrolled_speaker(samples)
    print(f"score={score:.3f} threshold={config.SPEAKER_THRESHOLD} -> {'MATCH' if matched else 'no match'}")


if __name__ == "__main__":
    import sys

    verify() if "--verify" in sys.argv else main()
