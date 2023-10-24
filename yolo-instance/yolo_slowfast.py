import argparse
import math
import os
import random
import time
import warnings

import cv2
import ffmpeg
import numpy as np
import pytorchvideo
import torch

warnings.filterwarnings("ignore", category=UserWarning)

from PIL import Image, ImageDraw, ImageFont
from pytorchvideo.data.ava import AvaLabeledVideoFramePaths
from pytorchvideo.models.hub import slowfast_r50_detection
from pytorchvideo.transforms.functional import (
    clip_boxes_to_image,
    short_side_scale_with_boxes,
    uniform_temporal_subsample,
)
from torchvision.transforms._functional_video import normalize

from deep_sort.deep_sort import DeepSort


class MyVideoCapture:
    def __init__(self, source):
        self.cap = cv2.VideoCapture(source)
        self.idx = -1
        self.end = False
        self.stack = []

    def read(self):
        self.idx += 1
        ret, img = self.cap.read()
        if ret:
            self.stack.append(img)
        else:
            self.end = True
        return ret, img

    def to_tensor(self, img):
        img = torch.from_numpy(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        return img.unsqueeze(0)

    def get_video_clip(self):
        assert len(self.stack) > 0, "clip length must large than 0 !"
        self.stack = [self.to_tensor(img) for img in self.stack]
        clip = torch.cat(self.stack).permute(-1, 0, 1, 2)
        del self.stack
        self.stack = []
        return clip

    def release(self):
        self.cap.release()

    def get(self, parameter):
        return self.cap.get(parameter)


def stream_ffmpeg(width, height, fps):
    process = (
        ffmpeg.input(
            "pipe:",
            format="rawvideo",
            codec="rawvideo",
            pix_fmt="bgr24",
            s=f"{width}x{height}",
            r=f"{fps}",
        )
        .output(
            "http://0.0.0.0:7001/yolo",
            codec="libx264",
            listen=1,
            pix_fmt="yuv420p",
            preset="ultrafast",
            f="flv",
        )
        .global_args("-re")
        .overwrite_output()
        .run_async(pipe_stdin=True)
    )
    return process


def tensor_to_numpy(tensor):
    img = tensor.cpu().numpy().transpose((1, 2, 0))
    return img


def ava_inference_transform(
    clip,
    boxes,
    num_frames=32,  # if using slowfast_r50_detection, change this to 32, 4 for slow
    crop_size=640,
    data_mean=[0.45, 0.45, 0.45],
    data_std=[0.225, 0.225, 0.225],
    slow_fast_alpha=4,  # if using slowfast_r50_detection, change this to 4, None for slow
):
    boxes = np.array(boxes)
    roi_boxes = boxes.copy()
    clip = uniform_temporal_subsample(clip, num_frames)
    clip = clip.float()
    clip = clip / 255.0
    height, width = clip.shape[2], clip.shape[3]
    boxes = clip_boxes_to_image(boxes, height, width)
    clip, boxes = short_side_scale_with_boxes(
        clip,
        size=crop_size,
        boxes=boxes,
    )
    clip = normalize(
        clip,
        np.array(data_mean, dtype=np.float32),
        np.array(data_std, dtype=np.float32),
    )
    boxes = clip_boxes_to_image(boxes, clip.shape[2], clip.shape[3])
    if slow_fast_alpha is not None:
        fast_pathway = clip
        slow_pathway = torch.index_select(
            clip,
            1,
            torch.linspace(
                0, clip.shape[1] - 1, clip.shape[1] // slow_fast_alpha
            ).long(),
        )
        clip = [slow_pathway, fast_pathway]

    return clip, torch.from_numpy(boxes), roi_boxes


def myPutText(src, text, pos, font_size, font_color):
    img_pil = Image.fromarray(src)
    draw = ImageDraw.Draw(img_pil)

    font_path = "./assets/Pretendard-Medium.ttf"
    font = ImageFont.truetype(font_path, 10)

    draw.text(pos, text, font=font, fill=font_color)

    return cv2.cvtColor(np.array(img_pil))


def plot_one_box(
    x,
    img,
    # color=[100, 100, 100],
    text_info="None",
    velocity=None,
    thickness=1,
    fontsize=0.5,
    fontthickness=1,
):
    # Plots one bounding box on image img
    color = [253, 253, 255]  # FDFDFF - GRADE1
    c1, c2 = (int(x[0]), int(x[1])), (int(x[2]), int(x[3]))

    cv2.rectangle(img, c1, c2, color, thickness, lineType=cv2.LINE_AA)

    t_size = cv2.getTextSize(
        text_info, cv2.FONT_HERSHEY_TRIPLEX, fontsize, fontthickness + 2
    )[0]

    cv2.rectangle(
        img, c1, (c1[0] + int(t_size[0]), c1[1] + int(t_size[1] * 1.45)), color, -1
    )

    # myPutText(
    #    img,
    #    text_info,
    #    (c1[0], c1[1] + t_size[1] + 2),
    #    fontsize,
    #    [135, 132, 154],  # 87849A - GRADE6
    # )

    # cv2.putText(
    #    img,
    #    text_info,
    #    (c1[0], c1[1] + t_size[1] + 2),
    #    cv2.FONT_HERSHEY_TRIPLEX,
    #    fontsize,
    #    [135, 132, 154],  # 87849A - GRADE6
    #    fontthickness,
    # )

    return img


def deepsort_update(Tracker, pred, xywh, np_img):
    outputs = Tracker.update(
        xywh, pred[:, 4:5], pred[:, 5].tolist(), cv2.cvtColor(np_img, cv2.COLOR_BGR2RGB)
    )
    return outputs


def save_yolopreds_tovideo(
    yolo_preds, id_to_ava_labels, color_map, output_video, ffmpeg_process, vis=False
):
    for i, (im, pred) in enumerate(zip(yolo_preds.ims, yolo_preds.pred)):
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)

        if pred.shape[0]:
            for j, (*box, cls, trackid, vx, vy) in enumerate(pred):
                if int(cls) != 0:
                    ava_label = ""

                elif trackid in id_to_ava_labels.keys():
                    ava_label = id_to_ava_labels[trackid].split(" ")[0]

                else:
                    ava_label = "Unknown"

                text = "{} {} {}".format(
                    int(trackid), yolo_preds.names[int(cls)], ava_label
                )
                color = color_map[int(cls)]
                im = plot_one_box(box, im, color, text)

        im = im.astype(np.uint8)
        im = cv2.cvtColor(im, cv2.COLOR_RGB2BGR)

        ffmpeg_process.stdin.write(im.tobytes())
        output_video.write(im)

        if vis:
            cv2.imshow("demo", im)


def main(config):
    device = config.device
    imsize = config.imsize

    model = torch.hub.load("ultralytics/yolov5", "yolov5l6").to(device)
    model.conf = config.conf
    model.iou = config.iou
    model.max_det = 100
    if config.classes:
        model.classes = config.classes

    video_model = slowfast_r50_detection(True).eval().to(device)

    deepsort_tracker = DeepSort("deep_sort/deep_sort/deep/checkpoint/ckpt.t7")
    ava_labelnames, _ = AvaLabeledVideoFramePaths.read_label_map("selfutils/temp.pbtxt")
    # coco_color_map = [[random.randint(0, 255) for _ in range(3)] for _ in range(80)]

    vide_save_path = config.output
    video = cv2.VideoCapture(config.input)
    width, height = int(video.get(3)), int(video.get(4))
    video.release()
    outputvideo = cv2.VideoWriter(
        vide_save_path, cv2.VideoWriter_fourcc(*"mp4v"), 25, (width, height)
    )
    print("processing...")

    cap = MyVideoCapture(config.input)
    id_to_ava_labels = {}
    a = time.time()

    camera_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    camera_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    camera_fps = cap.get(cv2.CAP_PROP_FPS)

    ffmpeg_process = stream_ffmpeg(camera_width, camera_height, camera_fps)

    while not cap.end:
        ret, img = cap.read()

        if not ret:
            continue

        yolo_preds = model([img], size=imsize)
        yolo_preds.files = ["img.jpg"]

        deepsort_outputs = []

        for j in range(len(yolo_preds.pred)):
            temp = deepsort_update(
                deepsort_tracker,
                yolo_preds.pred[j].cpu(),
                yolo_preds.xywh[j][:, 0:4].cpu(),
                yolo_preds.ims[j],
            )
            if len(temp) == 0:
                temp = np.ones((0, 8))
            deepsort_outputs.append(temp.astype(np.float32))

        yolo_preds.pred = deepsort_outputs

        if len(cap.stack) == 25:
            print(f"processing {cap.idx // 25}th second clips")

            clip = cap.get_video_clip()

            if yolo_preds.pred[0].shape[0]:
                inputs, inp_boxes, _ = ava_inference_transform(
                    clip, yolo_preds.pred[0][:, 0:4], crop_size=imsize
                )
                inp_boxes = torch.cat(
                    [torch.zeros(inp_boxes.shape[0], 1), inp_boxes], dim=1
                )

                if isinstance(inputs, list):
                    inputs = [inp.unsqueeze(0).to(device) for inp in inputs]
                else:
                    inputs = inputs.unsqueeze(0).to(device)

                with torch.no_grad():
                    slowfaster_preds = video_model(inputs, inp_boxes.to(device))
                    slowfaster_preds = slowfaster_preds.cpu()

                for tid, avalabel in zip(
                    yolo_preds.pred[0][:, 5].tolist(),
                    np.argmax(slowfaster_preds, axis=1).tolist(),
                ):
                    id_to_ava_labels[tid] = ava_labelnames[avalabel + 1]

        save_yolopreds_tovideo(
            yolo_preds,
            id_to_ava_labels,
            outputvideo,
            ffmpeg_process,
            config.show,
        )

    ffmpeg_process.stdin.close()
    ffmpeg_process.wait()

    print(
        "total cost: {:.3f} s, video length: {} s".format(time.time() - a, cap.idx / 25)
    )

    cap.release()
    outputvideo.release()
    print("saved video to:", vide_save_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=str,
        default="/home/wufan/images/video/vad.mp4",
        help="test imgs folder or video or camera",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output.mp4",
        help="folder to save result imgs, can not use input folder",
    )
    # object detect config
    parser.add_argument(
        "--imsize", type=int, default=640, help="inference size (pixels)"
    )
    parser.add_argument(
        "--conf", type=float, default=0.4, help="object confidence threshold"
    )
    parser.add_argument("--iou", type=float, default=0.4, help="IOU threshold for NMS")
    parser.add_argument(
        "--device", default="cuda", help="cuda device, i.e. 0 or 0,1,2,3 or cpu"
    )
    parser.add_argument(
        "--classes",
        nargs="+",
        type=int,
        help="filter by class: --class 0, or --class 0 2 3",
    )
    parser.add_argument("--show", action="store_true", help="show img")
    config = parser.parse_args()

    if config.input.isdigit():
        print("using local camera.")
        config.input = int(config.input)

    print(config)
    main(config)
