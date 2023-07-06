from os import listdir, path
import numpy as np
import scipy, cv2, os, sys, argparse, audio
import json, subprocess, random, string
from tqdm import tqdm
from glob import glob
import torch, face_detection
from wav2lip_models import Wav2Lip
import platform
from face_parsing import init_parser, swap_regions
from esrgan.upsample import upscale
from esrgan.upsample import load_sr
from basicsr.archs.rrdbnet_arch import RRDBNet
from basicsr.utils.download_util import load_file_from_url

parser = argparse.ArgumentParser(description='Inference code to lip-sync videos in the wild using Wav2Lip models')

parser.add_argument('--checkpoint_path', type=str, default="checkpoints/wav2lip_gan.pth",
					help='Name of saved checkpoint to load weights from', required=False)

parser.add_argument('--segmentation_path', type=str, default="checkpoints/face_segmentation.pth",
					help='Name of saved checkpoint of segmentation network', required=False)

parser.add_argument('--sr_path', type=str, default='weights/4x_BigFace_v3_Clear.pth', 
					help='Name of saved checkpoint of super-resolution network', required=False)

parser.add_argument('--face', type=str, 
					help='Filepath of video/image that contains faces to use', required=True)
parser.add_argument('--audio', type=str, 
					help='Filepath of video/audio file to use as raw audio source', required=True)
parser.add_argument('--outfile', type=str, help='Video path to save result. See default for an e.g.', 
								default='results/result_voice.mp4')


parser.add_argument('--static', type=bool, 
					help='If True, then use only first video frame for inference', default=False)
parser.add_argument('--fps', type=float, help='Can be specified only if input is a static image (default: 25)', 
					default=25., required=False)

parser.add_argument('--pads', nargs='+', type=int, default=[0, 10, 0, 0], 
					help='Padding (top, bottom, left, right). Please adjust to include chin at least')

parser.add_argument('--face_det_batch_size', type=int, 
					help='Batch size for face detection', default=16)
parser.add_argument('--wav2lip_batch_size', type=int, help='Batch size for Wav2Lip model(s)', default=128)

parser.add_argument('--resize_factor', default=1, type=int, 
			help='Reduce the resolution by this factor. Sometimes, best results are obtained at 480p or 720p')

parser.add_argument('--crop', nargs='+', type=int, default=[0, -1, 0, -1], 
					help='Crop video to a smaller region (top, bottom, left, right). Applied after resize_factor and rotate arg. ' 
					'Useful if multiple face present. -1 implies the value will be auto-inferred based on height, width')

parser.add_argument('--box', nargs='+', type=int, default=[-1, -1, -1, -1], 
					help='Specify a constant bounding box for the face. Use only as a last resort if the face is not detected.'
					'Also, might work only if the face is not moving around much. Syntax: (top, bottom, left, right).')

parser.add_argument('--rotate', default=False, action='store_true',
					help='Sometimes videos taken from a phone can be flipped 90deg. If true, will flip video right by 90deg.'
					'Use if you get a flipped result, despite feeding a normal looking video')

parser.add_argument('--nosmooth', default=False, action='store_true',
					help='Prevent smoothing face detections over a short temporal window')
parser.add_argument('--no_seg', default=False, action='store_true',
					help='Prevent using face segmentation')
parser.add_argument('--no_sr', default=False, action='store_true',
					help='Prevent using super resolution')
parser.add_argument('--enhance_face', default=None, choices=['gfpgan','codeformer'],
					help='Use GFP-GAN to enhance facial details.')
parser.add_argument('-w', '--fidelity_weight', type=float, default=0.75, 
            				help='Balance the quality and fidelity. Default: 0.75')
parser.add_argument('--save_frames', default=False, action='store_true',
					help='Save each frame as an image. Use with caution')
parser.add_argument('--gt_path', type=str, 
					help='Where to store saved ground truth frames', required=False)
parser.add_argument('--pred_path', type=str, 
					help='Where to store frames produced by algorithm', required=False)
parser.add_argument('--save_as_video', action="store_true", default=False,
					help='Whether to save frames as video', required=False)
parser.add_argument('--image_prefix', type=str, default="",
					help='Prefix to save frames with', required=False)

args = parser.parse_args()
args.img_size = 96

if os.path.isfile(args.face) and args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
	args.static = True

def get_smoothened_boxes(boxes, T):
	for i in range(len(boxes)):
		if i + T > len(boxes):
			window = boxes[len(boxes) - T:]
		else:
			window = boxes[i : i + T]
		boxes[i] = np.mean(window, axis=0)
	return boxes

def face_detect(images):
	detector = face_detection.FaceAlignment(face_detection.LandmarksType._2D, 
											flip_input=False, device=device)

	batch_size = args.face_det_batch_size
	
	while 1:
		predictions = []
		try:
			for i in range(0, len(images), batch_size):
				predictions.extend(detector.get_detections_for_batch(np.array(images[i:i + batch_size])))
		except RuntimeError:
			if batch_size == 1: 
				raise RuntimeError('Image too big to run face detection on GPU. Please use the --resize_factor argument')
			batch_size //= 2
			print('Recovering from OOM error; New batch size: {}'.format(batch_size))
			continue
		break

	results = []
	pady1, pady2, padx1, padx2 = args.pads
	for rect, image in zip(predictions, images):
		if rect is None:
			continue
		y1 = max(0, rect[1] - pady1)
		y2 = min(image.shape[0], rect[3] + pady2)
		x1 = max(0, rect[0] - padx1)
		x2 = min(image.shape[1], rect[2] + padx2)
		
		results.append([x1, y1, x2, y2])

	boxes = np.array(results)
	if not args.nosmooth: boxes = get_smoothened_boxes(boxes, T=5)
	results = [[image[y1: y2, x1:x2], (y1, y2, x1, x2)] for image, (x1, y1, x2, y2) in zip(images, boxes)]

	del detector
	return results 

def datagen(mels):
    img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    # Uncommented code removed for clarity

    reader = read_frames()

    for i, m in enumerate(mels):
        try:
            frame_to_save = next(reader)
        except StopIteration:
            reader = read_frames()
            frame_to_save = next(reader, None)

        if frame_to_save is not None:
            face_detect_result = face_detect([frame_to_save])
            if len(face_detect_result) > 0:  # Check if face detection was successful
                face, coords = face_detect_result[0]
                face = cv2.resize(face, (args.img_size, args.img_size))
                img_batch.append(face)
                mel_batch.append(m)
                frame_batch.append(frame_to_save)
                coords_batch.append(coords)

        if len(img_batch) >= args.wav2lip_batch_size:
            img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

            img_masked = img_batch.copy()
            img_masked[:, args.img_size // 2:] = 0

            img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
            mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

            yield img_batch, mel_batch, frame_batch, coords_batch
            img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    if len(img_batch) > 0:
        img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

        img_masked = img_batch.copy()
        img_masked[:, args.img_size // 2:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

        yield img_batch, mel_batch, frame_batch, coords_batch

mel_step_size = 16
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} for inference.'.format(device))

def _load(checkpoint_path):
	if device == 'cuda':
		checkpoint = torch.load(checkpoint_path)
	else:
		checkpoint = torch.load(checkpoint_path,
								map_location=lambda storage, loc: storage)
	return checkpoint

def load_model(path):
	model = Wav2Lip()
	print("Load checkpoint from: {}".format(path))
	checkpoint = _load(path)
	s = checkpoint["state_dict"]
	new_s = {}
	for k, v in s.items():
		new_s[k.replace('module.', '')] = v
	model.load_state_dict(new_s)

	model = model.to(device)
	return model.eval()

def read_frames():
	if args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
		face = cv2.imread(args.face)
		while 1:
			yield face

	video_stream = cv2.VideoCapture(args.face)
	fps = video_stream.get(cv2.CAP_PROP_FPS)

	print('Reading video frames from start...')

	while 1:
		still_reading, frame = video_stream.read()
		if not still_reading:
			video_stream.release()
			break
		if args.resize_factor > 1:
			frame = cv2.resize(frame, (frame.shape[1]//args.resize_factor, frame.shape[0]//args.resize_factor))

		if args.rotate:
			frame = cv2.rotate(frame, cv2.cv2.ROTATE_90_CLOCKWISE)

		y1, y2, x1, x2 = args.crop
		if x2 == -1: x2 = frame.shape[1]
		if y2 == -1: y2 = frame.shape[0]

		frame = frame[y1:y2, x1:x2]

		yield frame

def main():
	if not os.path.isfile(args.face):
		raise ValueError('--face argument must be a valid path to video/image file')

	elif args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
		fps = args.fps
	else:
		video_stream = cv2.VideoCapture(args.face)
		fps = video_stream.get(cv2.CAP_PROP_FPS)
		video_stream.release()


	if not args.audio.endswith('.wav'):
		print('Extracting raw audio...')
		command = 'ffmpeg -y -i {} -strict -2 {}'.format(args.audio, 'temp/temp.wav')

		subprocess.call(command, shell=True)
		args.audio = 'temp/temp.wav'

	wav = audio.load_wav(args.audio, 16000)
	mel = audio.melspectrogram(wav)
	print(mel.shape)

	if np.isnan(mel.reshape(-1)).sum() > 0:
		raise ValueError('Mel contains nan! Using a TTS voice? Add a small epsilon noise to the wav file and try again')

	mel_chunks = []
	mel_idx_multiplier = 80./fps 
	i = 0
	while 1:
		start_idx = int(i * mel_idx_multiplier)
		if start_idx + mel_step_size > len(mel[0]):
			mel_chunks.append(mel[:, len(mel[0]) - mel_step_size:])
			break
		mel_chunks.append(mel[:, start_idx : start_idx + mel_step_size])
		i += 1

	print("Length of mel chunks: {}".format(len(mel_chunks)))

	batch_size = args.wav2lip_batch_size
	gen = datagen(mel_chunks)



	if args.save_as_video:
		gt_out = cv2.VideoWriter("temp/gt.avi", cv2.VideoWriter_fourcc(*'DIVX'), fps, (384, 384))
		pred_out = cv2.VideoWriter("temp/pred.avi", cv2.VideoWriter_fourcc(*'DIVX'), fps, (96, 96))

	abs_idx = 0
	for i, (img_batch, mel_batch, frames, coords) in enumerate(tqdm(gen, 
											total=int(np.ceil(float(len(mel_chunks))/batch_size)))):
		if i == 0:
			if not args.no_seg==True:
				print("Loading segmentation network...")
				seg_net = load_file_from_url(
					url='https://github.com/GucciFlipFlops1917/wav2lip-hq-updated-ESRGAN/releases/download/v0.0.1/face_segmentation.pth',
					model_dir='checkpoints', progress=True, file_name=None)
				seg_net = init_parser(args.segmentation_path)
			if not args.no_sr==True:
				print("Loading super resolution model...")
				run_params = load_sr(args.sr_path, device, args.enhance_face)
			
			model_path = load_file_from_url(
				url='https://github.com/GucciFlipFlops1917/wav2lip-hq-updated-ESRGAN/releases/download/v0.0.1/wav2lip_gan.pth',
				model_dir='checkpoints', progress=True, file_name=None)
			model = load_model(args.checkpoint_path)
			print ("Model loaded")

			frame_h, frame_w = next(read_frames()).shape[:-1]
			out = cv2.VideoWriter('temp/result.avi', 
									cv2.VideoWriter_fourcc(*'DIVX'), fps, (frame_w, frame_h))

		img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
		mel_batch = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(device)

		with torch.no_grad():
			pred = model(mel_batch, img_batch)

		pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.
		
		for p, f, c in zip(pred, frames, coords):
			y1, y2, x1, x2 = c

			if args.save_frames:
				if args.save_as_video:
					pred_out.write(p.astype(np.uint8))
					gt_out.write(cv2.resize(f[y1:y2, x1:x2], (384, 384)))
				else:
					cv2.imwrite(f"{args.gt_path}/{args.image_prefix}{abs_idx}.png", f[y1:y2, x1:x2])
					cv2.imwrite(f"{args.pred_path}/{args.image_prefix}{abs_idx}.png", p)
					abs_idx += 1

			if not args.no_sr:
				if args.enhance_face==None:
					p = upscale(p, 0, run_params)
				elif args.enhance_face=='codeformer':
					p = upscale(p, 2, [run_params, device, args.fidelity_weight])
				elif args.enhance_face=='gfpgan':
					p = upscale(p, 1, run_params)
			p = cv2.resize(p.astype(np.uint8), (x2 - x1, y2 - y1))
			
			if args.no_seg==False:
				p = swap_regions(f[y1:y2, x1:x2], p, seg_net)

			f[y1:y2, x1:x2] = p
			out.write(f)

	out.release()

	command = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(args.audio, 'temp/result.avi', args.outfile)
	subprocess.call(command, shell=platform.system() != 'Windows')

	if args.save_frames and args.save_as_video:
		gt_out.release()
		pred_out.release()

		command = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(args.audio, 'temp/gt.avi', args.gt_path)
		subprocess.call(command, shell=platform.system() != 'Windows')

		command = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(args.audio, 'temp/pred.avi', args.pred_path)
		subprocess.call(command, shell=platform.system() != 'Windows')


if __name__ == '__main__':
	main()
