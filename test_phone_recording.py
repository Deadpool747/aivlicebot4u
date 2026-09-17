import tempfile, wave, unittest
from pathlib import Path
import numpy as np
from phone_recording import CallRecording
class RecordingTests(unittest.TestCase):
 def test_both_sides_are_saved_at_correct_rate(self):
  with tempfile.TemporaryDirectory() as directory:
   rec=CallRecording(Path(directory),'huzaifa',clock=lambda:0)
   rec.add(np.full(1600,1000,dtype='<i2').tobytes(),16000,0)
   rec.add(np.full(2400,2000,dtype='<i2').tobytes(),24000,1)
   key=rec.save();file=next(Path(directory).rglob(key+'.wav'))
   with wave.open(str(file),'rb') as stream:
    self.assertEqual((stream.getnchannels(),stream.getframerate(),stream.getnframes()),(2,24000,2400))
    data=np.frombuffer(stream.readframes(2400),dtype='<i2').reshape(-1,2)
    self.assertTrue((data[:,0]==1000).all());self.assertTrue((data[:,1]==2000).all())
 def test_empty_call_has_no_recording(self):
  self.assertIsNone(CallRecording(Path('.'),'test').save())
 def test_burst_packets_do_not_overlap_or_clip(self):
  with tempfile.TemporaryDirectory() as directory:
   rec=CallRecording(Path(directory),'test',clock=lambda:0)
   pcm=np.full(480,20000,dtype='<i2').tobytes()
   for _ in range(10): rec.add(pcm,24000,1)
   key=rec.save()
   with wave.open(str(next(Path(directory).rglob(key+'.wav'))),'rb') as stream:
    self.assertEqual(stream.getnframes(),4800)
    data=np.frombuffer(stream.readframes(4800),dtype='<i2').reshape(-1,2)
    self.assertTrue((data[:,1]==20000).all())
 def test_real_pause_is_preserved(self):
  now=[0.0];rec=CallRecording(Path('.'),'test',clock=lambda:now[0])
  pcm=np.ones(480,dtype='<i2').tobytes();rec.add(pcm,24000,1)
  now[0]=1.0;rec.add(pcm,24000,1)
  self.assertEqual(rec.parts[1][0],24000)
if __name__ == '__main__': unittest.main()

