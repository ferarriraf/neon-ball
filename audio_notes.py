"""Construction de la bande-son : chaque événement joue la note suivante.

La musique source est découpée séquentiellement en tranches de
note_duration_ms : au 1er rebond la tranche 1, au 2e la tranche 2, etc.
C'est ce qui fait "jouer la mélodie" par la balle. Décodage et encodage
audio via le binaire ffmpeg embarqué par imageio-ffmpeg (rien à installer
sur le serveur).
"""

import logging
import subprocess
import wave

import numpy as np
from imageio_ffmpeg import get_ffmpeg_exe

log = logging.getLogger("audio")

SAMPLE_RATE = 44100


def decode_song(path, max_seconds=180):
    """Décode n'importe quel format audio en PCM stéréo 16 bits / 44,1 kHz.

    max_seconds borne la mémoire : la balle ne joue de toute façon que
    quelques dizaines de secondes de musique par vidéo.
    """
    cmd = [get_ffmpeg_exe(), "-v", "error", "-i", path,
           "-t", str(max_seconds),
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ac", "2", "-ar", str(SAMPLE_RATE), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
    if len(pcm) == 0:
        raise ValueError(f"Aucun audio décodé depuis {path}")
    return pcm


def _mix(track, sound, start):
    """Ajoute `sound` dans `track` à partir de l'échantillon `start`."""
    end = min(start + len(sound), len(track))
    if end > start:
        track[start:end] += sound[:end - start]


def build_note_track(pcm, events, note_duration_ms, total_duration_s,
                     start_offset_s=0):
    """Place la tranche n de la musique au moment du n-ième événement.

    events : liste de (temps, type) où type vaut bounce / escape / complete.
    start_offset_s permet de sauter une intro : le découpage commence à cet
    endroit du morceau (et y reboucle si le morceau est épuisé).
    """
    note_samples = int(SAMPLE_RATE * note_duration_ms / 1000)
    offset = min(int(SAMPLE_RATE * start_offset_s), max(0, len(pcm) - note_samples))
    total_samples = int(SAMPLE_RATE * total_duration_s)
    track = np.zeros((total_samples, 2), dtype=np.int32)

    # Enveloppe anti-clic : fade-in bref, fade-out sur la moitié de la note.
    envelope = np.ones(note_samples)
    fade_in = int(SAMPLE_RATE * 0.008)
    fade_out = note_samples // 2
    envelope[:fade_in] = np.linspace(0, 1, fade_in)
    envelope[-fade_out:] = np.linspace(1, 0, fade_out)
    envelope = envelope[:, None]

    pos = offset
    for t, kind in events:
        start = int(t * SAMPLE_RATE)
        if start >= total_samples:
            break
        if kind == "complete":
            _mix(track, synth_fanfare(), start)
            continue
        if pos + note_samples > len(pcm):
            pos = offset  # la musique est épuisée : on reboucle
        note = (pcm[pos:pos + note_samples].astype(np.float32) * envelope).astype(np.int32)
        pos += note_samples
        _mix(track, note, start)
        if kind == "escape":
            _mix(track, synth_sparkle(), start)

    np.clip(track, -32768, 32767, out=track)
    return track.astype(np.int16)


def midi_to_freq(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def load_midi_melody(path):
    """Extrait la mélodie d'un fichier MIDI (liste de fréquences en Hz).

    Algorithme "skyline" : toutes les pistes sauf la batterie sont fusionnées
    et, quand plusieurs notes démarrent en même temps (accord), on garde la
    plus aiguë — c'est presque toujours la mélodie chantée/connue.
    """
    import mido
    events = []
    t = 0.0
    for msg in mido.MidiFile(path):  # itération = temps en secondes, tempo géré
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and getattr(msg, "channel", 0) != 9:
            events.append((t, msg.note))
    events.sort()
    melody = []
    for start, pitch in events:
        if melody and start - melody[-1][0] < 0.035:
            if pitch > melody[-1][1]:
                melody[-1] = (start, pitch)
        else:
            melody.append((start, pitch))
    return [midi_to_freq(p) for _, p in melody]


def synth_note(freq, duration_s):
    """Note synthétisée façon piano électrique : attaque nette, décroissance douce."""
    n = int(SAMPLE_RATE * duration_s)
    t = np.arange(n) / SAMPLE_RATE
    env = np.exp(-t * 5.0) * np.minimum(1.0, t * 300)
    wave = (np.sin(2 * np.pi * freq * t)
            + 0.4 * np.sin(4 * np.pi * freq * t) * np.exp(-t * 8)
            + 0.2 * np.sin(6 * np.pi * freq * t) * np.exp(-t * 14))
    mono = (wave * env * 0.35 * 32767).astype(np.int32)
    return np.stack([mono, mono], axis=1)


def synth_sparkle(base_freq=1046.5):
    """Petit arpège cristallin "bling" joué en franchissant un anneau."""
    parts = []
    for i, ratio in enumerate((1.0, 1.26, 1.5, 2.0)):  # majeur ascendant
        note = synth_note(base_freq * ratio, 0.5) // 3
        pad = np.zeros((int(SAMPLE_RATE * 0.045 * i), 2), dtype=np.int32)
        parts.append(np.concatenate([pad, note]))
    length = max(len(p) for p in parts)
    out = np.zeros((length, 2), dtype=np.int32)
    for p in parts:
        out[:len(p)] += p
    return out


def synth_fanfare(base_freq=523.25):
    """Célébration : arpège majeur montant + accord final, à chaque sphère finie."""
    parts = []
    steps = ((1.0, 0.0), (1.26, 0.09), (1.5, 0.18), (2.0, 0.27),
             (2.52, 0.36), (3.0, 0.45))
    for ratio, delay in steps:
        note = synth_note(base_freq * ratio, 1.1) // 2
        pad = np.zeros((int(SAMPLE_RATE * delay), 2), dtype=np.int32)
        parts.append(np.concatenate([pad, note]))
    # Accord plaqué pour finir.
    for ratio in (2.0, 2.52, 3.0, 4.0):
        note = synth_note(base_freq * ratio, 1.4) // 3
        pad = np.zeros((int(SAMPLE_RATE * 0.6), 2), dtype=np.int32)
        parts.append(np.concatenate([pad, note]))
    length = max(len(p) for p in parts)
    out = np.zeros((length, 2), dtype=np.int32)
    for p in parts:
        out[:len(p)] += p
    return out


def build_midi_track(freqs, events, note_duration_ms, total_duration_s):
    """Joue la note n de la mélodie au moment du n-ième événement.

    events : liste de (temps, type). Un franchissement d'anneau ajoute un
    "bling" par-dessus la note, une sphère terminée déclenche la fanfare.
    """
    total_samples = int(SAMPLE_RATE * total_duration_s)
    track = np.zeros((total_samples, 2), dtype=np.int32)
    if not freqs:
        return track.astype(np.int16)
    # Les notes sonnent un peu plus longtemps que la tranche audio pour
    # laisser la décroissance respirer.
    duration = min(0.9, max(0.35, note_duration_ms / 1000 * 1.6))
    index = 0
    for t, kind in events:
        start = int(t * SAMPLE_RATE)
        if start >= total_samples:
            break
        if kind == "complete":
            _mix(track, synth_fanfare(), start)
            continue
        _mix(track, synth_note(freqs[index % len(freqs)], duration), start)
        index += 1
        if kind == "escape":
            _mix(track, synth_sparkle(), start)

    np.clip(track, -32768, 32767, out=track)
    return track.astype(np.int16)


def write_wav(path, pcm):
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())


def mux(video_path, wav_path, out_path):
    """Assemble la vidéo muette et la bande-son en un mp4 final."""
    cmd = [get_ffmpeg_exe(), "-y", "-v", "error",
           "-i", video_path, "-i", wav_path,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", out_path]
    subprocess.run(cmd, check=True, capture_output=True)
