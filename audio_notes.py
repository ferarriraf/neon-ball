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
        # Seuls les rebonds consomment une tranche de musique.
        if kind == "escape":
            _mix(track, synth_sparkle(), start)
            continue
        if kind == "complete":
            _mix(track, synth_fanfare(), start)
            continue
        if pos + note_samples > len(pcm):
            break  # morceau épuisé : on ne le redémarre pas en cours de vidéo
        note = (pcm[pos:pos + note_samples].astype(np.float32) * envelope).astype(np.int32)
        pos += note_samples
        _mix(track, note, start)

    np.clip(track, -32768, 32767, out=track)
    return track.astype(np.int16)


# Un haut-parleur de téléphone ne restitue quasiment rien sous ~150 Hz : une
# note grave est jouée à plein niveau mais reste inaudible, ce qui donne
# l'impression que la balle rebondit sans faire de bruit. La mélodie est donc
# transposée dans un registre qui passe partout.
AUDIBLE_LOW = 200.0
AUDIBLE_HIGH = 2000.0
TARGET_CENTER = 520.0     # autour de do5, bien clair sur un petit haut-parleur


def midi_to_freq(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def _transpose_to_audible(freqs):
    """Remonte la mélodie dans un registre audible sur un téléphone.

    D'abord un décalage d'octaves appliqué à TOUTES les notes (le contour
    mélodique est conservé intact), puis, seulement pour les rares notes qui
    dépassent encore, un repli à l'octave.
    """
    if not freqs:
        return freqs
    ordered = sorted(freqs)
    median = ordered[len(ordered) // 2]
    shift = 1.0
    while median * shift < TARGET_CENTER / 1.5:
        shift *= 2
    while median * shift > TARGET_CENTER * 1.5:
        shift /= 2

    out = []
    for f in freqs:
        f *= shift
        while f < AUDIBLE_LOW:
            f *= 2
        while f > AUDIBLE_HIGH:
            f /= 2
        out.append(f)
    return out


# Une note répétée plus que ça EN TÊTE de morceau est un ostinato d'intro,
# pas la mélodie : on saute cette boucle pour attaquer directement le thème.
# Les répétitions à l'intérieur du morceau sont toujours conservées telles
# quelles — elles font partie de la musique.
INTRO_REPEAT_LIMIT = 4
MAX_INTRO_TRIM_RATIO = 0.4

# Sélection du canal mélodique.
MIN_SHARE_OF_BUSIEST = 0.15
MIN_MELODY_NOTES = 8
# En dessous, la mélodie tournerait en boucle toutes les quelques secondes :
# signe que le mauvais canal a été retenu, on reprend tout le fichier.
MIN_USABLE_MELODY = 12


def _pick_melody_channel(by_channel):
    """Choisit le canal qui porte la mélodie.

    Un accompagnement martèle souvent la même note (ostinato d'intro) : on
    privilégie donc le canal à la fois aigu et varié, sinon la balle joue
    l'accompagnement et la mélodie connue n'arrive jamais.
    """
    if not by_channel:
        return None
    # Un canal doit peser un minimum face au plus fourni pour être un thème :
    # sinon un bruitage de quelques notes très variées l'emporte (cas vécu sur
    # "The Final Countdown" : 14 notes battaient la mélodie de 820 notes).
    busiest = max(len(n) for n in by_channel.values())
    floor = max(MIN_MELODY_NOTES, MIN_SHARE_OF_BUSIEST * busiest)
    candidates = {c: n for c, n in by_channel.items() if len(n) >= floor}
    if not candidates:                       # morceau très court
        candidates = by_channel

    best_score, best_channel = None, None
    for channel, notes in candidates.items():
        pitches = [p for _, p in notes]
        variety = len(set(pitches)) / len(pitches)
        # La mélodie est presque toujours la voix du dessus ; la variété
        # départage sans pouvoir écraser ce critère.
        score = sum(pitches) / len(pitches) + 25 * variety
        if best_score is None or score > best_score:
            best_score, best_channel = score, channel
    return best_channel


def load_midi_melody(path):
    """Extrait la mélodie d'un fichier MIDI (liste de fréquences en Hz).

    1. On isole le canal le plus mélodique (hors batterie).
    2. Skyline : sur un accord, on garde la note la plus aiguë.
    3. On limite les répétitions d'une même note à la suite, pour ne pas
       gaspiller de longues secondes sur une note tenue ou martelée.
    """
    import mido
    events = []
    t = 0.0
    for msg in mido.MidiFile(path):  # itération = temps en secondes, tempo géré
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and getattr(msg, "channel", 0) != 9:
            events.append((t, msg.note, msg.channel))
    if not events:
        return []

    by_channel = {}
    for start, pitch, channel in events:
        by_channel.setdefault(channel, []).append((start, pitch))
    channel = _pick_melody_channel(by_channel)
    chosen = by_channel.get(channel) or [(s, p) for s, p, _ in events]

    pitches = _skyline(chosen)
    if len(pitches) < MIN_USABLE_MELODY and len(by_channel) > 1:
        # Le canal retenu est trop maigre : on repart du fichier entier
        # plutôt que de faire tourner trois notes en boucle.
        pitches = _skyline([(s, p) for s, p, _ in events])
    return _transpose_to_audible(
        [midi_to_freq(p) for p in _trim_intro_ostinato(pitches)])


def _skyline(notes):
    """Sur des notes simultanées (accord), ne garde que la plus aiguë."""
    melody = []
    for start, pitch in sorted(notes):
        if melody and start - melody[-1][0] < 0.035:
            if pitch > melody[-1][1]:
                melody[-1] = (start, pitch)
        else:
            melody.append((start, pitch))
    return [pitch for _, pitch in melody]


def _trim_intro_ostinato(pitches):
    """Saute une note martelée en boucle au tout début (intro), rien d'autre."""
    start = 0
    limit = int(len(pitches) * MAX_INTRO_TRIM_RATIO)
    while start < limit:
        run = 1
        while start + run < len(pitches) and pitches[start + run] == pitches[start]:
            run += 1
        if run <= INTRO_REPEAT_LIMIT:
            break
        start += run
    return pitches[start:]


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
    """Petit arpège cristallin "bling" joué en franchissant un anneau.

    Un franchissement ne joue aucune note de mélodie : ce carillon est le
    SEUL son de l'événement, il doit donc s'entendre franchement — sinon le
    passage d'une couche paraît muet.
    """
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
        note = synth_note(base_freq * ratio, 1.1) // 3
        pad = np.zeros((int(SAMPLE_RATE * delay), 2), dtype=np.int32)
        parts.append(np.concatenate([pad, note]))
    # Accord plaqué pour finir.
    for ratio in (2.0, 2.52, 3.0, 4.0):
        note = synth_note(base_freq * ratio, 1.4) // 4
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
    # Les notes résonnent nettement plus longtemps que leur tranche : la
    # décroissance comble les écarts entre deux rebonds au lieu de laisser
    # un trou audible.
    duration = min(1.3, max(0.5, note_duration_ms / 1000 * 2.6))
    index = 0
    for t, kind in events:
        start = int(t * SAMPLE_RATE)
        if start >= total_samples:
            break
        # Seuls les rebonds font avancer la mélodie. Un anneau franchi ou une
        # sphère terminée ne joue QUE son effet sonore : aucune note n'est
        # consommée, la mélodie reprend où elle en était au rebond suivant.
        if kind == "escape":
            _mix(track, synth_sparkle(), start)
            continue
        if kind == "complete":
            _mix(track, synth_fanfare(), start)
            continue
        if index >= len(freqs):
            break  # mélodie terminée : on ne la rejoue pas depuis le début
        _mix(track, synth_note(freqs[index], duration), start)
        index += 1

    np.clip(track, -32768, 32767, out=track)
    return track.astype(np.int16)


def frame_spectrum(pcm, fps, frame_count, bands=22):
    """Spectre par image pour le visualiseur (frame_count x bands, 0..1).

    Bandes espacées logarithmiquement (comme l'oreille), normalisées sur le
    maximum du morceau, avec une décroissance douce pour éviter le
    clignotement d'une image à l'autre.
    """
    mono = pcm.mean(axis=1).astype(np.float32)
    window = 2048
    hann = np.hanning(window).astype(np.float32)
    # Plage resserrée sur le contenu réel des notes : au-delà, les bandes ne
    # portent que du bruit numérique.
    edges = np.geomspace(60, 5000, bands + 1) * window / SAMPLE_RATE
    edges = np.clip(edges.astype(int), 1, window // 2 - 1)

    raw = np.zeros((frame_count, bands), dtype=np.float32)
    for i in range(frame_count):
        # Fenêtre centrée sur l'image : sinon l'analyse regarde 46 ms en avant
        # et les barres s'allument avant qu'on entende la note.
        start = int(i / fps * SAMPLE_RATE) - window // 2
        seg = mono[max(0, start):max(0, start) + window]
        if len(seg) < window:
            seg = np.pad(seg, (window - len(seg), 0) if start < 0
                         else (0, window - len(seg)))
        mag = np.abs(np.fft.rfft(seg * hann))
        for b in range(bands):
            lo, hi = edges[b], max(edges[b] + 1, edges[b + 1])
            raw[i, b] = mag[lo:hi].mean()

    # Normalisation par bande pour que les aigus restent visibles, MAIS avec
    # un plancher : sans lui, une bande qui ne contient rien est divisée par
    # son propre maximum minuscule et le moindre résidu numérique devient une
    # barre pleine — la dernière barre s'allumait ainsi en plein silence.
    global_peak = max(raw.max(), 1e-6)
    band_peaks = np.maximum(raw.max(axis=0), global_peak * 0.05)
    raw = np.sqrt(raw / band_peaks)  # échelle perceptuelle
    # Lissage entre bandes voisines pour un visuel continu.
    raw[:, 1:-1] = (raw[:, :-2] + 2 * raw[:, 1:-1] + raw[:, 2:]) / 4
    # Attaque immédiate, retombée progressive.
    for i in range(1, frame_count):
        np.maximum(raw[i], raw[i - 1] * 0.82, out=raw[i])
    return raw


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
