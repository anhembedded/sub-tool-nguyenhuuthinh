"""Standard-library native PCM clock checks for the isolated Ava environment."""
from fractions import Fraction
import wave

RATE=48000


def samples_at_frame(frame,fps='30/1'):
    if type(frame) is not int or frame<0:raise ValueError('Frame boundaries must be nonnegative integers')
    rate=Fraction(fps)
    if rate<=0:raise ValueError('FPS must be positive')
    return round(Fraction(frame*RATE,1)/rate)


def pcm_info(path,*,channels=None,samples=None):
    with wave.open(str(path),'rb') as audio:
        info={'sample_rate':audio.getframerate(),'channels':audio.getnchannels(),'sample_width':audio.getsampwidth(),
              'samples':audio.getnframes(),'compression':audio.getcomptype()}
        count=0
        while block:=audio.readframes(RATE*8):count+=len(block)
        if count!=info['samples']*info['sample_width']*info['channels']:raise ValueError('Truncated/misaligned PCM payload')
    if (info['sample_rate'],info['sample_width'],info['compression'])!=(RATE,2,'NONE'):raise ValueError('Expected native PCM48 s16')
    if channels is not None and info['channels']!=channels:raise ValueError('PCM channel count differs')
    if samples is not None and info['samples']!=samples:raise ValueError('PCM sample count differs')
    if info['channels'] not in (1,2):raise ValueError('Only mono/stereo PCM is accepted')
    if info['samples']<=0:raise ValueError('Empty PCM payload')
    return info
