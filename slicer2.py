import numpy as np
import ass
import librosa
from tqdm import tqdm
import os
import soundfile
# This function is obtained from librosa.


def get_rms(
    y,
    *,
    frame_length=2048,
    hop_length=512,
    pad_mode="constant",
):
    padding = (int(frame_length // 2), int(frame_length // 2))
    y = np.pad(y, padding, mode=pad_mode)

    axis = -1
    # put our new within-frame axis at the end for now
    out_strides = y.strides + tuple([y.strides[axis]])
    # Reduce the shape on the framing axis
    x_shape_trimmed = list(y.shape)
    x_shape_trimmed[axis] -= frame_length - 1
    out_shape = tuple(x_shape_trimmed) + tuple([frame_length])
    xw = np.lib.stride_tricks.as_strided(
        y, shape=out_shape, strides=out_strides
    )
    if axis < 0:
        target_axis = axis - 1
    else:
        target_axis = axis + 1
    xw = np.moveaxis(xw, -1, target_axis)
    # Downsample along the target axis
    slices = [slice(None)] * xw.ndim
    slices[axis] = slice(0, None, hop_length)
    x = xw[tuple(slices)]

    # Calculate power
    power = np.mean(np.abs(x) ** 2, axis=-2, keepdims=True)

    return np.sqrt(power)


class Slicer:
    def __init__(self,
                 sr: int,
                 threshold: float = -40.,
                 min_length: int = 5000,
                 min_interval: int = 300,
                 hop_size: int = 20,
                 max_sil_kept: int = 5000,
                 ass_path: str = '',        # 保存ass字幕的路径（字符串为空则不保存）
                 f0_ass: bool = False,      # 是否计算每个切片的f0
                 f0_filter: int = 330,      # 只保留平均f0大于filter的切片
                 f0_log: bool = False,      # 是否打印f0信息
                 f0_progress: bool = True,
                 clip_path: str = ''        # 保存切片的路径（字符串为空则不保存）
                 ):
        if not min_length >= min_interval >= hop_size:
            raise ValueError(
                'The following condition must be satisfied: min_length >= min_interval >= hop_size')
        if not max_sil_kept >= hop_size:
            raise ValueError(
                'The following condition must be satisfied: max_sil_kept >= hop_size')
        min_interval = sr * min_interval / 1000
        self.threshold = 10 ** (threshold / 20.)
        self.hop_size = round(sr * hop_size / 1000)
        self.win_size = min(round(min_interval), 4 * self.hop_size)
        self.min_length = round(sr * min_length / 1000 / self.hop_size)
        self.min_interval = round(min_interval / self.hop_size)
        self.max_sil_kept = round(sr * max_sil_kept / 1000 / self.hop_size)
        self.ass_path = ass_path
        self.f0_ass = f0_ass
        self.f0_filter = f0_filter
        self.sr = sr
        self.f0_progress = f0_progress
        self.f0_log = f0_log
        self.clip_path = clip_path

    def _apply_slice(self, waveform, begin, end):
        if len(waveform.shape) > 1:
            return waveform[:, begin * self.hop_size: min(waveform.shape[1], end * self.hop_size)]
        else:
            return waveform[begin * self.hop_size: min(waveform.shape[0], end * self.hop_size)]

    # @timeit
    def slice(self, waveform):
        if len(waveform.shape) > 1:
            samples = waveform.mean(axis=0)
        else:
            samples = waveform
        if samples.shape[0] <= self.min_length:
            return [waveform]
        rms_list = get_rms(y=samples, frame_length=self.win_size,
                           hop_length=self.hop_size).squeeze(0)
        sil_tags = []
        silence_start = None
        clip_start = 0
        for i, rms in enumerate(rms_list):
            # Keep looping while frame is silent.
            if rms < self.threshold:
                # Record start of silent frames.
                if silence_start is None:
                    silence_start = i
                continue
            # Keep looping while frame is not silent and silence start has not been recorded.
            if silence_start is None:
                continue
            # Clear recorded silence start if interval is not enough or clip is too short
            is_leading_silence = silence_start == 0 and i > self.max_sil_kept
            need_slice_middle = i - silence_start >= self.min_interval and i - \
                clip_start >= self.min_length
            if not is_leading_silence and not need_slice_middle:
                silence_start = None
                continue
            # Need slicing. Record the range of silent frames to be removed.
            if i - silence_start <= self.max_sil_kept:
                pos = rms_list[silence_start: i + 1].argmin() + silence_start
                if silence_start == 0:
                    sil_tags.append((0, pos))
                else:
                    sil_tags.append((pos, pos))
                clip_start = pos
            elif i - silence_start <= self.max_sil_kept * 2:
                pos = rms_list[i - self.max_sil_kept: silence_start +
                               self.max_sil_kept + 1].argmin()
                pos += i - self.max_sil_kept
                pos_l = rms_list[silence_start: silence_start +
                                 self.max_sil_kept + 1].argmin() + silence_start
                pos_r = rms_list[i - self.max_sil_kept: i +
                                 1].argmin() + i - self.max_sil_kept
                if silence_start == 0:
                    sil_tags.append((0, pos_r))
                    clip_start = pos_r
                else:
                    sil_tags.append((min(pos_l, pos), max(pos_r, pos)))
                    clip_start = max(pos_r, pos)
            else:
                pos_l = rms_list[silence_start: silence_start +
                                 self.max_sil_kept + 1].argmin() + silence_start
                pos_r = rms_list[i - self.max_sil_kept: i +
                                 1].argmin() + i - self.max_sil_kept
                if silence_start == 0:
                    sil_tags.append((0, pos_r))
                else:
                    sil_tags.append((pos_l, pos_r))
                clip_start = pos_r
            silence_start = None
        # Deal with trailing silence.
        total_frames = rms_list.shape[0]
        if silence_start is not None and total_frames - silence_start >= self.min_interval:
            silence_end = min(total_frames, silence_start + self.max_sil_kept)
            pos = rms_list[silence_start: silence_end +
                           1].argmin() + silence_start
            sil_tags.append((pos, total_frames + 1))

        # 创建ass字幕文件对象
        doc = ass.document.Document()
        # 设置脚本信息
        # subs.info = ass.document.Info(
        #     play_res_x=800,
        #     play_res_y=600,
        #     timer=100
        # )
        style = ass.section.Style()
        style.fontsize = 18
        style.primary_color = ass.data.Color(r=0xff, g=0xff, b=0xff)
        style.secondary_color = ass.data.Color(r=0x00, g=0x00, b=0x00)
        style.margin_v = -1
        style.alignment = 8
        style.shadow = 0
        style.outline = 0
        doc.styles.append(style)
        print("clip_path:", self.clip_path)
        # Apply and return slices.
        if len(sil_tags) == 0:
            return [waveform]
        else:
            chunks = []
            if sil_tags[0][0] > 0:
                chunks.append(self._apply_slice(waveform, 0, sil_tags[0][0]))
            for i in tqdm(range(len(sil_tags)-1), desc="Auto Slicer"):
                # for i in range(len(sil_tags) - 1):
                y = self._apply_slice(
                    waveform, sil_tags[i][1], sil_tags[i + 1][0])

                event = ass_event(
                    y, self.sr, sil_tags[i][1], sil_tags[i + 1][0], self.f0_ass, self.f0_filter)
                # 将事件添加到字幕文件
                doc.events.append(event)
                print(i, event)
                if event.__class__.__name__ == "Dialog":
                    chunks.append(y)
                    if len(self.clip_path) > 0:
                        if self.f0_ass:
                            output_path = "{}{}_{}.wav".format(
                                self.clip_path, i, event.text)
                        else:
                            output_path = "{}{}.wav".format(self.clip_path, i)
                        print("output_path:", output_path)
                        soundfile.write(output_path, y, self.sr)

            if sil_tags[-1][1] < total_frames:
                chunks.append(self._apply_slice(
                    waveform, sil_tags[-1][1], total_frames))

            if len(self.ass_path) > 0:
                # 保存为ass文件
                with open(self.ass_path, mode="w", encoding="utf-8") as f:
                    doc.dump_file(f)
            return chunks


def ass_event(y, sr, time_start, time_end, f0_ass=False, f0_filter=0, f0_log=False):
    formatted_f0_mean = ""

    event = None
    if f0_ass:

        # 读取音频文件，提取时间序列和采样率
        #   y, sr = librosa.load(wav_file)
        # 计算音频文件的 f0
        f0, voiced_flag, voiced_probs = librosa.pyin(
            y, fmin=librosa.note_to_hz('C2'), fmax=librosa.note_to_hz('C7'))
        # 将未检测到声音的帧的 f0 置为 0
        f0[voiced_flag == 0] = 0
        # 筛选出符合条件的 f0
        f0 = f0[(f0 > 50) & (f0 < 1100)]

        # 统计大于 filter 的比例
        f0_h = (f0 > f0_filter).astype(int)
        proportion = np.mean(f0_h)
        # 计算 f0 平均值并保留一位小数
        f0_mean = np.mean(f0)
        formatted_f0_mean = format(f0_mean, '.1f')

        if f0_log:
            print('平均f0：{}, 大于filter({})的f0占比{:.2%}'.format(
                formatted_f0_mean, f0_filter, proportion))

        if f0_mean >= f0_filter:
            # 创建一个事件
            event = ass.document.Dialogue(
                layer=0,
                start=time_start,
                end=time_end,
                style="Default",
                text=formatted_f0_mean
            )
        else:
            event = ass.document.Comment(
                layer=0,
                start=time_start,
                end=time_end,
                style="Default",
                text=formatted_f0_mean
            )
    else:
        event = ass.document.Dialogue(
            layer=0,
            start=time_start,
            end=time_end,
            style="Default",
            text=''
        )
    return event


def main():
    import os.path
    from argparse import ArgumentParser

    import librosa
    import soundfile

    parser = ArgumentParser()
    parser.add_argument('audio', type=str, help='The audio to be sliced')
    parser.add_argument('--out', type=str,
                        help='Output directory of the sliced audio clips')
    parser.add_argument('--db_thresh', type=float, required=False, default=-40,
                        help='The dB threshold for silence detection')
    parser.add_argument('--min_length', type=int, required=False, default=5000,
                        help='The minimum milliseconds required for each sliced audio clip')
    parser.add_argument('--min_interval', type=int, required=False, default=300,
                        help='The minimum milliseconds for a silence part to be sliced')
    parser.add_argument('--hop_size', type=int, required=False, default=10,
                        help='Frame length in milliseconds')
    parser.add_argument('--max_sil_kept', type=int, required=False, default=500,
                        help='The maximum silence length kept around the sliced clip, presented in milliseconds')
    args = parser.parse_args()
    out = args.out
    if out is None:
        out = os.path.dirname(os.path.abspath(args.audio))
    audio, sr = librosa.load(args.audio, sr=None, mono=False)
    slicer = Slicer(
        sr=sr,
        threshold=args.db_thresh,
        min_length=args.min_length,
        min_interval=args.min_interval,
        hop_size=args.hop_size,
        max_sil_kept=args.max_sil_kept
    )
    chunks = slicer.slice(audio)
    if not os.path.exists(out):
        os.makedirs(out)
    for i, chunk in enumerate(chunks):
        if len(chunk.shape) > 1:
            chunk = chunk.T
        soundfile.write(os.path.join(out, f'%s_%d.wav' % (
            os.path.basename(args.audio).rsplit('.', maxsplit=1)[0], i)), chunk, sr)


if __name__ == '__main__':
    main()
