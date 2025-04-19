# -*- coding: utf-8 -*-
# @Author  : LG

from PyQt5 import QtWidgets, QtCore, QtGui
from ISAT.ui.MainWindow import Ui_MainWindow
from ISAT.widgets.category_setting_dialog import CategorySettingDialog
from ISAT.widgets.category_edit_dialog import CategoryEditDialog
from ISAT.widgets.category_dock_widget import CategoriesDockWidget
from ISAT.widgets.annos_dock_widget import AnnosDockWidget
from ISAT.widgets.files_dock_widget import FilesDockWidget
from ISAT.widgets.info_dock_widget import InfoDockWidget
from ISAT.widgets.right_button_menu import RightButtonMenu
from ISAT.widgets.shortcut_dialog import ShortcutDialog
from ISAT.widgets.about_dialog import AboutDialog
from ISAT.widgets.setting_dialog import SettingDialog
from ISAT.widgets.converter_dialog import ConverterDialog
from ISAT.widgets.video_to_frames_dialog import Video2FramesDialog
from ISAT.widgets.auto_segment_dialog import AutoSegmentDialog
from ISAT.widgets.model_manager_dialog import ModelManagerDialog
from ISAT.widgets.annos_validator_dialog import AnnosValidatorDialog
from ISAT.widgets.canvas import AnnotationScene, AnnotationView
from ISAT.configs import STATUSMode, MAPMode, load_config, save_config, CONFIG_FILE, SOFTWARE_CONFIG_FILE, CHECKPOINT_PATH, ISAT_ROOT, SHORTCUT_FILE
from ISAT.annotation import Object, Annotation
from ISAT.widgets.polygon import Polygon, PromptPoint
from ISAT.configs import STATUSMode, CLICKMode, DRAWMode, CONTOURMode
import os
from PIL import Image
import functools
import imgviz
from ISAT.segment_any.segment_any import SegAny, SegAnyVideo
from ISAT.segment_any.gpu_resource import GPUResource_Thread, osplatform
import ISAT.icons_rc
from PyQt5.QtCore import QThread, pyqtSignal
import numpy as np
import torch
import cv2  # 调整图像饱和度
import datetime
from skimage.draw.draw import polygon


class QtBoxStyleProgressBar(QtWidgets.QProgressBar):
    # copy from qtbox
    def __init__(self):
        super(QtBoxStyleProgressBar, self).__init__()
        self.setTextVisible(False)
        self.setStyleSheet("""
        QProgressBar {
            border: 2px solid #888783;
            border-radius: 5px;
        }

        QProgressBar::chunk {
            background-color: #74d65f;
            border-radius: 2px;
            width: 9px;
            margin: 0.5px;
        }
        """)


def calculate_area(points):
    area = 0
    num_points = len(points)
    for i in range(num_points):
        p1 = points[i]
        p2 = points[(i + 1) % num_points]
        d = p1[0] * p2[1] - p2[0] * p1[1]
        area += d
    return abs(area) / 2


class SegAnyThread(QThread):
    tag = pyqtSignal(int, int, str)

    def __init__(self, mainwindow):
        super(SegAnyThread, self).__init__()
        self.mainwindow = mainwindow
        self.results_dict = {}
        self.index = None

    @torch.no_grad()
    def sam_encoder(self, image):
        torch.cuda.empty_cache()
        with torch.inference_mode(), torch.autocast(self.mainwindow.segany.device,
                                                    dtype=self.mainwindow.segany.model_dtype,
                                                    enabled=torch.cuda.is_available()):

            # sam2 函数命名等发生很大改变，为了适应后续基于sam2的各类模型，这里分开处理sam1和sam2模型
            if 'sam2' in self.mainwindow.segany.model_type:
                _orig_hw = tuple([image.shape[:2]])
                input_image = self.mainwindow.segany.predictor_with_point_prompt._transforms(image)
                input_image = input_image[None, ...].to(self.mainwindow.segany.predictor_with_point_prompt.device)
                backbone_out = self.mainwindow.segany.predictor_with_point_prompt.model.forward_image(input_image)
                _, vision_feats, _, _ = self.mainwindow.segany.predictor_with_point_prompt.model._prepare_backbone_features(backbone_out)
                if self.mainwindow.segany.predictor_with_point_prompt.model.directly_add_no_mem_embed:
                    vision_feats[-1] = vision_feats[-1] + self.mainwindow.segany.predictor_with_point_prompt.model.no_mem_embed
                feats = [
                    feat.permute(1, 2, 0).view(1, -1, *feat_size)
                    for feat, feat_size in zip(vision_feats[::-1], self.mainwindow.segany.predictor_with_point_prompt._bb_feat_sizes[::-1])
                ][::-1]
                _features = {"image_embed": feats[-1], "high_res_feats": tuple(feats[:-1])}
                return _features, _orig_hw, _orig_hw
            else:
                input_image = self.mainwindow.segany.predictor_with_point_prompt.transform.apply_image(image)
                input_image_torch = torch.as_tensor(input_image, device=self.mainwindow.segany.predictor_with_point_prompt.device)
                input_image_torch = input_image_torch.permute(2, 0, 1).contiguous()[None, :, :, :]

                original_size = image.shape[:2]
                input_size = tuple(input_image_torch.shape[-2:])

                input_image = self.mainwindow.segany.predictor_with_point_prompt.model.preprocess(input_image_torch)
                features = self.mainwindow.segany.predictor_with_point_prompt.model.image_encoder(input_image)
                return features, original_size, input_size

    def run(self):
        if self.index is not None:

            # 需要缓存特征的图像索引，可以自行更改缓存策略
            indexs = [self.index]
            if self.index + 1 < len(self.mainwindow.files_list):
                indexs += [self.index + 1]
            if self.index - 1 > -1:
                indexs += [self.index - 1]

            # 先删除不需要的旧特征
            features_ks = list(self.results_dict.keys())
            for k in features_ks:
                if k not in indexs:
                    try:
                        del self.results_dict[k]
                        self.tag.emit(k, 0, '')  # 删除
                    except:
                        pass

            for index in indexs:
                if index not in self.results_dict:
                    self.tag.emit(index, 2, '')    # 进行

                    image_path = os.path.join(self.mainwindow.image_root, self.mainwindow.files_list[index])
                    self.results_dict[index] = {}
                    # image_data = cv2.imread(image_path)
                    image_data = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8),cv2.IMREAD_COLOR)
                    image_data = cv2.cvtColor(image_data, cv2.COLOR_BGR2RGB)
                    try:
                        features, original_size, input_size = self.sam_encoder(image_data)
                    except Exception as e:
                        self.tag.emit(index, 3, '{}'.format(e))  # error
                        del self.results_dict[index]
                        continue

                    self.results_dict[index]['features'] = features
                    self.results_dict[index]['original_size'] = original_size
                    self.results_dict[index]['input_size'] = input_size

                    self.tag.emit(index, 1, '')    # 完成

                    torch.cuda.empty_cache()
                else:
                    self.tag.emit(index, 1, '')


class SegAnyVideoThread(QThread):
    tag = pyqtSignal(int, int, bool, bool, str)    # current, total, finished, is_error, message

    def __init__(self, mainwindow):
        super(SegAnyVideoThread, self).__init__()
        self.mainwindow = mainwindow
        self.start_frame_idx = 0
        self.max_frame_num_to_track = None

    def run(self):
        print('self.start_frame_idx: ', self.start_frame_idx)
        print('self.max_frame_num_to_track: ', self.max_frame_num_to_track)

        if self.max_frame_num_to_track is not None:
            total = self.max_frame_num_to_track
        else:
            total = len(self.mainwindow.files_list) - self.start_frame_idx + 1

        with torch.inference_mode(), torch.autocast(self.mainwindow.segany_video.device,
                                                    dtype=self.mainwindow.segany_video.model_dtype):

            if not self.mainwindow.use_segment_anything_video:
                self.mainwindow.actionVideo_segment.setEnabled(False)
                self.mainwindow.actionVideo_segment_once.setEnabled(False)
                self.mainwindow.actionVideo_segment_five_times.setEnabled(False)
                return

            if self.mainwindow.segany_video.inference_state == {}:
                self.mainwindow.segany_video.init_state(self.mainwindow.image_root, self.mainwindow.files_list)
            self.mainwindow.segany_video.reset_state()

            current_file = self.mainwindow.files_list[self.start_frame_idx]
            current_file_path = os.path.join(self.mainwindow.image_root, current_file)
            current_label_path = os.path.join(self.mainwindow.label_root, '.'.join(current_file.split('.')[:-1]) + '.json')
            current_label = Annotation(current_file_path, current_label_path)

            current_label.load_annotation()

            group_object_dict = {}

            for object in current_label.objects:
                group = int(object.group)
                segmentation = [(int(p[1]), int(p[0])) for p in object.segmentation]
                category = object.category
                is_crowd = object.iscrowd
                layer = object.layer
                note = object.note

                # fill mask
                mask = np.zeros(shape=(current_label.height, current_label.width), dtype=np.uint8)
                xs = [x for x, y in segmentation]
                ys = [y for x, y in segmentation]
                rr, cc = polygon(xs, ys, mask.shape)
                mask[rr, cc] = 1

                if group not in group_object_dict:
                    group_object_dict[group] = {}
                    group_object_dict[group]['mask'] = mask
                    group_object_dict[group]['category'] = category
                    group_object_dict[group]['is_crowd'] = is_crowd
                    group_object_dict[group]['layer'] = layer
                    group_object_dict[group]['note'] = note
                else:
                    group_object_dict[group]['mask'] = group_object_dict[group]['mask'] + mask

            if len(group_object_dict) < 1:
                self.tag.emit(0, total, True, True, 'Please label objects before video segment.')
                return
            try:
                for group, object_dict in group_object_dict.items():
                    mask = object_dict['mask']
                    self.mainwindow.segany_video.add_new_mask(self.start_frame_idx, group, mask)

                for index, (out_frame_idxs, out_obj_ids, out_mask_logits) in enumerate(self.mainwindow.segany_video.predictor.propagate_in_video(
                        self.mainwindow.segany_video.inference_state,
                        start_frame_idx=self.start_frame_idx,
                        max_frame_num_to_track=self.max_frame_num_to_track,
                        reverse=False,
                )):
                    if index == 0:  # 忽略当前图片
                        continue
                    file = self.mainwindow.files_list[out_frame_idxs]
                    file_path = os.path.join(self.mainwindow.image_root, file)
                    label_path = os.path.join(self.mainwindow.label_root, '.'.join(file.split('.')[:-1]) + '.json')
                    annotation = Annotation(file_path, label_path)

                    objects = []
                    for index_mask, out_obj_id in enumerate(out_obj_ids):

                        masks = out_mask_logits[index_mask]   # [1, h, w]
                        masks = masks > 0
                        masks = masks.cpu().numpy()

                        # mask to polygon
                        masks = masks.astype('uint8') * 255
                        h, w = masks.shape[-2:]
                        masks = masks.reshape(h, w)

                        if self.mainwindow.scene.contour_mode == CONTOURMode.SAVE_ALL:
                            # 当保留所有轮廓时，检测所有轮廓，并建立二层等级关系
                            contours, hierarchy = cv2.findContours(masks, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_TC89_KCOS)
                        else:
                            # 当只保留外轮廓或单个mask时，只检测外轮廓
                            contours, hierarchy = cv2.findContours(masks, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_KCOS)

                        if self.mainwindow.scene.contour_mode == CONTOURMode.SAVE_MAX_ONLY and contours:
                            largest_contour = max(contours, key=cv2.contourArea)  # 只保留面积最大的轮廓
                            contours = [largest_contour]

                        for contour in contours:
                            # polydp
                            if self.mainwindow.cfg['software']['use_polydp']:
                                epsilon_factor = 0.001
                                epsilon = epsilon_factor * cv2.arcLength(contour, True)
                                contour = cv2.approxPolyDP(contour, epsilon, True)

                            if len(contour) < 3:
                                continue

                            segmentation = []
                            xmin, ymin, xmax, ymax = annotation.width, annotation.height, 0, 0
                            for point in contour:
                                x, y = point[0]
                                x, y = float(x), float(y)
                                xmin = min(x, xmin)
                                ymin = min(x, ymin)
                                xmax = max(y, xmax)
                                ymax = max(y, ymax)

                                segmentation.append((x, y))

                            area = calculate_area(segmentation)
                            # bbox = (xmin, ymin, xmax, ymax)
                            bbox = None
                            obj = Object(category=group_object_dict[out_obj_id]['category'],
                                         group=out_obj_id,
                                         segmentation=segmentation,
                                         area=area,
                                         layer=group_object_dict[out_obj_id]['layer'],
                                         bbox=bbox,
                                         iscrowd=group_object_dict[out_obj_id]['is_crowd'],
                                         note=group_object_dict[out_obj_id]['note'])
                            objects.append(obj)

                    annotation.objects = objects
                    annotation.save_annotation()
                    self.tag.emit(index, total, False, False, '')

                self.tag.emit(index, total, True, False, '')

            except Exception as e:
                self.tag.emit(index, total, True, True, '{}'.format(e))


class InitSegAnyThread(QThread):
    tag = pyqtSignal(bool, bool)

    def __init__(self, mainwindow):
        super(InitSegAnyThread, self).__init__()
        self.mainwindow = mainwindow
        self.model_path: str = None

    def run(self):
        sam_tag = False
        sam_video_tag = False
        if self.model_path is not None:
            try:
                self.mainwindow.segany = SegAny(self.model_path, self.mainwindow.cfg['software']['use_bfloat16'])
                sam_tag = True
            except Exception as e:
                print('Init SAM Error: ', e)
                sam_tag = False
            if 'sam2' in self.model_path:
                try:
                    self.mainwindow.segany_video = SegAnyVideo(self.model_path, self.mainwindow.cfg['software']['use_bfloat16'])
                    sam_video_tag = True
                except Exception as e:
                    print('Init SAM2 video Error: ', e)
                    sam_video_tag = False

        self.tag.emit(sam_tag, sam_video_tag)


class CheckLatestVersionThread(QThread):
    tag = pyqtSignal(bool, str)
    def __init__(self, mainwindow):
        super(CheckLatestVersionThread, self).__init__()
        self.mainwindow = mainwindow

    def run(self):
        try:
            import requests
            from ISAT import __version__

            response = requests.get(f"https://pypi.org/pypi/isat-sam/json", timeout=3)
            latest_version = response.json()["info"]["version"]
            self.tag.emit(__version__ == latest_version, latest_version)
        except Exception as e:
            self.tag.emit(True, '')


class MainWindow(QtWidgets.QMainWindow, Ui_MainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.setupUi(self)

        self.image_root: str = None
        self.label_root: str = None

        self.files_list: list = []
        self.current_index = None
        self.current_file_index: int = None

        self.current_label = '__background__'
        self.current_group = 1

        self.config_file = CONFIG_FILE
        self.software_config_file = SOFTWARE_CONFIG_FILE
        self.shortcut_config_file = SHORTCUT_FILE

        self.saved = True

        self.can_be_annotated = True
        self.load_finished = False
        self.polygons: list = []

        self.png_palette = None # 图像拥有调色盘，说明是单通道的标注png文件
        self.instance_cmap = imgviz.label_colormap()
        self.map_mode = MAPMode.LABEL
        # 标注目标
        self.current_label: Annotation = None
        self.use_segment_anything = False
        self.use_segment_anything_video = False
        self.gpu_resource_thread = None

        # 标注模式下，多边形不可见
        self.create_mode_invisible_polygon = True

        # 新增 手动/自动 group选择
        self.group_select_mode = 'auto'
        self.init_ui()
        self.reload_cfg()

        self.init_connect()
        self.reset_action()

        # sam初始化线程，大模型加载较慢
        self.init_segany_thread = InitSegAnyThread(self)
        self.init_segany_thread.tag.connect(self.init_sam_finish)

        # 检查最新版本
        self.check_latest_version_thread = CheckLatestVersionThread(self)
        self.check_latest_version_thread.tag.connect(self.latest_version_tip)
        self.check_latest_version_thread.start()

        self.filter_annotated = False  #weijinge,用于切换图片时只跳到有json的图片的flag
    
        self.actionToggleFilter.setChecked(False)
        self.actionToggleFilter.toggled.connect(self.toggle_filter_mode)
       



    def init_segment_anything(self, model_name=None, checked=None):
        if checked is not None and not checked:
            return

        if not self.saved:
            result = QtWidgets.QMessageBox.question(self, 'Warning', 'Proceed without saved?', QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No, QtWidgets.QMessageBox.StandardButton.No)
            if result == QtWidgets.QMessageBox.StandardButton.No:
                if isinstance(self.sender(), QtWidgets.QAction):
                    self.sender().setChecked(False)
                return
        if model_name is None:
            if self.use_segment_anything:
                model_name = os.path.split(self.segany.checkpoint)[-1]
            else:
                return
        # 等待sam线程完成
        self.actionSegment_anything_point.setEnabled(False)
        self.actionSegment_anything_box.setEnabled(False)
        try:
            self.seganythread.wait()
            self.seganythread.results_dict.clear()
        except:
            if isinstance(self.sender(), QtWidgets.QAction):
                self.sender().setChecked(False)

        if model_name == '':
            self.use_segment_anything = False
            self.model_manager_dialog.update_ui()
            return
        model_path = os.path.join(CHECKPOINT_PATH, model_name)
        if not os.path.exists(model_path):
            QtWidgets.QMessageBox.warning(self, 'Warning',
                                          'The checkpoint of [Segment anything] not existed. If you want use quick annotate, please download from {}'.format(
                                              'https://github.com/facebookresearch/segment-anything#model-checkpoints'))
            self.use_segment_anything = False
            self.model_manager_dialog.update_ui()
            return

        self.init_segany_thread.model_path = model_path
        self.init_segany_thread.start()
        self.setEnabled(False)

    def init_sam_finish(self, sam_tag:bool, sam_video_tag:bool):
        print('sam_tag:', sam_tag, 'sam_video_tag: ', sam_video_tag)
        self.setEnabled(True)
        if sam_video_tag:
            self.use_segment_anything_video = True
            if self.files_list:
                self.segany_video.init_state(self.image_root, self.files_list)

            self.segany_video_thread = SegAnyVideoThread(self)
            self.segany_video_thread.tag.connect(self.seg_video_finish)

            # sam2 建议使用bfloat16
            if self.segany.model_dtype == torch.float32:
                if self.cfg['software']['language'] == 'zh':
                    QtWidgets.QMessageBox.warning(self,
                                                  'warning',
                                                  """建议使用bfloat16模式进行视频分割\n在[设置]界面打开该功能""")
                else:
                    QtWidgets.QMessageBox.warning(self,
                                                  'warning',
                                                  """Suggest Use bfloat16 mode to segment video.\nYou can open it in [Setting].""")

        else:
            self.segany_video_thread = None
            self.use_segment_anything_video = False
            torch.cuda.empty_cache()

        self.actionVideo_segment.setEnabled(self.use_segment_anything_video)
        self.actionVideo_segment_once.setEnabled(self.use_segment_anything_video)
        self.actionVideo_segment_five_times.setEnabled(self.use_segment_anything_video)

        if sam_tag:
            self.use_segment_anything = True
            if self.use_segment_anything:
                if self.segany.device != 'cpu':
                    if self.gpu_resource_thread is None:
                        self.gpu_resource_thread = GPUResource_Thread()
                        self.gpu_resource_thread.message.connect(self.labelGPUResource.setText)
                        self.gpu_resource_thread.start()
                else:
                    self.labelGPUResource.setText('cpu')
            else:
                self.labelGPUResource.setText('segment anything unused.')
            tooltip = 'model: {}'.format(os.path.split(self.segany.checkpoint)[-1])
            tooltip += '\ndtype: {}'.format(self.segany.model_dtype)
            tooltip += '\ntorch: {}'.format(torch.__version__)
            if self.segany.device == 'cuda':
                try:
                    tooltip += '\ncuda : {}'.format(torch.version.cuda)
                except: 
                    pass
            self.labelGPUResource.setToolTip(tooltip)

            self.seganythread = SegAnyThread(self)
            self.seganythread.tag.connect(self.sam_encoder_finish)
            self.seganythread.start()

            if self.current_index is not None:
                self.show_image(self.current_index)

            checkpoint_name = os.path.split(self.segany.checkpoint)[-1]
            self.statusbar.showMessage('Use the checkpoint named {}.'.format(checkpoint_name), 3000)
        else:
            self.use_segment_anything = False

        self.model_manager_dialog.update_ui()

    def sam_encoder_finish(self, index:int, state:int, message:str):
        if state == 1:  # 识别完
            # 如果当前图片刚识别完，需刷新segany状态
            if self.current_index == index:
                self.SeganyEnabled()

        # 图片识别状态刷新
        if state == 1: color = '#00FF00'
        elif state == 0: color = '#999999'
        elif state == 2: color = '#FFFF00'
        elif state == 3:
            color = '#999999'
            if index == self.current_index:
                QtWidgets.QMessageBox.warning(self, 'warning','SAM not support the image: {}\nError: {}'.format(self.files_list[index], message))

        else: color = '#999999'

        if index == self.current_index:
            self.files_dock_widget.label_current_state.setStyleSheet("background-color: {};".format(color))
        elif index == self.current_index - 1:
            self.files_dock_widget.label_prev_state.setStyleSheet("background-color: {};".format(color))
        elif index == self.current_index + 1:
            self.files_dock_widget.label_next_state.setStyleSheet("background-color: {};".format(color))
        else:
            pass

        # item = self.files_dock_widget.listWidget.item(index)
        # widget = self.files_dock_widget.listWidget.itemWidget(item)
        # if widget is not None:
        #     state_color = widget.findChild(QtWidgets.QLabel, 'state_color')
        #     state_color.setStyleSheet("background-color: {};".format(color))

    def SeganyEnabled(self):
        """
        segany激活
        判断当前图片是否缓存特征图，如果存在特征图，设置segany参数，并开放半自动标注
        :return:
        """
        if not self.use_segment_anything:
            self.actionSegment_anything_point.setEnabled(False)
            self.actionSegment_anything_box.setEnabled(False)
            return

        results = self.seganythread.results_dict.get(self.current_index, {})
        features = results.get('features', None)
        original_size = results.get('original_size', None)
        input_size = results.get('input_size', None)

        if features is not None and original_size is not None and input_size is not None:
            if self.segany.model_source == 'sam_hq':
                features, interm_features = features
                self.segany.predictor_with_point_prompt.interm_features = interm_features
            self.segany.predictor_with_point_prompt.features = features
            self.segany.predictor_with_point_prompt.original_size = original_size
            self.segany.predictor_with_point_prompt.input_size = input_size
            self.segany.predictor_with_point_prompt.is_image_set = True
            # sam2
            self.segany.predictor_with_point_prompt._orig_hw = list(original_size)
            self.segany.predictor_with_point_prompt._features = features
            self.segany.predictor_with_point_prompt._is_image_set = True

            self.actionSegment_anything_point.setEnabled(True)
            self.actionSegment_anything_box.setEnabled(True)
        else:
            self.segany.predictor_with_point_prompt.reset_image()
            self.actionSegment_anything_point.setEnabled(False)
            self.actionSegment_anything_box.setEnabled(False)

    def seg_video_start(self, max_frame_num_to_track=None):
        if self.current_index == None:
            return

        if not self.saved:
            QtWidgets.QMessageBox.warning(self, 'Warning', 'Current annotation has not been saved!')
            return

        self.setEnabled(False)
        self.statusbar_change_status(is_message=False)
        self.segany_video_thread.start_frame_idx = self.current_index
        self.segany_video_thread.max_frame_num_to_track=max_frame_num_to_track
        self.segany_video_thread.start()

    def seg_video_finish(self, current, total, finished, is_error, message):
        if is_error:
            QtWidgets.QMessageBox.warning(self, 'warning', message)

        print('Segment video: {}/{}'.format(current, total))
        self.progressbar.setMaximum(total)
        self.progressbar.setValue(current)
        if finished:
            self.statusbar_change_status(is_message=True)
            self.progressbar.setValue(0)
            self.setEnabled(True)

    def init_ui(self):
        self.category_setting_dialog = CategorySettingDialog(parent=self, mainwindow=self)

        self.categories_dock_widget = CategoriesDockWidget(mainwindow=self)
        self.categories_dock.setWidget(self.categories_dock_widget)

        self.annos_dock_widget = AnnosDockWidget(mainwindow=self)
        self.annos_dock.setWidget(self.annos_dock_widget)

        self.files_dock_widget = FilesDockWidget(mainwindow=self)
        self.files_dock.setWidget(self.files_dock_widget)

        self.info_dock_widget = InfoDockWidget(mainwindow=self)
        self.info_dock.setWidget(self.info_dock_widget)

        self.model_manager_dialog = ModelManagerDialog(self, self)

        self.scene = AnnotationScene(mainwindow=self)
        self.category_edit_widget = CategoryEditDialog(self, self, self.scene)

        self.Converter_dialog = ConverterDialog(self, mainwindow=self)
        self.video2frames_dialog = Video2FramesDialog(self, self)
        self.auto_segment_dialog = AutoSegmentDialog(self, self)
        self.annos_validator_dialog = AnnosValidatorDialog(self, self)

        self.view = AnnotationView(parent=self)
        self.view.setScene(self.scene)
        self.setCentralWidget(self.view)

        self.right_button_menu = RightButtonMenu(mainwindow=self)
        self.right_button_menu.addAction(self.actionEdit)
        self.right_button_menu.addAction(self.actionCopy)
        self.right_button_menu.addAction(self.actionTo_top)
        self.right_button_menu.addAction(self.actionTo_bottom)
        self.right_button_menu.addAction(self.actionDelete)

        self.shortcut_dialog = ShortcutDialog(self, self)
        self.about_dialog = AboutDialog(self)
        self.setting_dialog = SettingDialog(self, self)

        self.labelGPUResource = QtWidgets.QLabel('')
        self.labelGPUResource.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.labelGPUResource.setFixedWidth(180)
        self.statusbar.addPermanentWidget(self.labelGPUResource)

        self.labelCoord = QtWidgets.QLabel('')
        self.labelCoord.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.labelCoord.setFixedWidth(150)
        self.statusbar.addPermanentWidget(self.labelCoord)

        self.labelData = QtWidgets.QLabel('')
        self.labelData.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.labelData.setFixedWidth(150)
        self.statusbar.addPermanentWidget(self.labelData)

        # mode显示, view, create, edit, repaint
        self.modeState = QtWidgets.QLabel('V')
        self.modeState.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.modeState.setFixedWidth(50)
        self.modeState.setStyleSheet("""
            background-color: #70AEFF;
            border-radius : 5px; 
            color: white;
        """)
        self.statusbar.addPermanentWidget(self.modeState)

        self.progressbar = QtBoxStyleProgressBar()
        self.progressbar.setTextVisible(False)
        self.progressbar.setFixedWidth(500)
        self.progressbar.setVisible(False)
        self.statusbar.addPermanentWidget(self.progressbar)

        self.update_menuSAM()

        # image saturation  调整图像饱和度
        self.toolBar.addSeparator()
        self.image_saturation = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
        self.image_saturation.setFixedWidth(50)
        self.image_saturation.setStatusTip('Image saturation.')
        self.image_saturation.setToolTip('Image saturation')
        self.image_saturation.setMaximum(500)
        self.image_saturation.setMinimum(0)
        self.image_saturation.setPageStep(10)
        self.image_saturation.setTickInterval(10)
        self.image_saturation.valueChanged.connect(self.change_saturation)
        self.toolBar.addWidget(self.image_saturation)

        # 右侧工具栏，添加弹性空间
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.toolBar_2.insertWidget(self.actionLanguage, spacer)

        self.trans = QtCore.QTranslator()

    def statusbar_change_status(self, is_message:bool=True):
        self.labelGPUResource.setVisible(is_message)
        self.labelData.setVisible(is_message)
        self.labelCoord.setVisible(is_message)
        self.modeState.setVisible(is_message)
        self.progressbar.setVisible(not is_message)

    def update_menuSAM(self):
        #
        self.menuSAM_model.clear()
        self.menuSAM_model.addAction(self.actionModel_manage)
        model_names = sorted(
            [pth for pth in os.listdir(CHECKPOINT_PATH) if pth.endswith('.pth') or pth.endswith('.pt')])
        self.pths_actions = {}
        # for model_name in model_names:
        #     action = QtWidgets.QAction(self)
        #     action.setObjectName("actionZoom_in")
        #     action.triggered.connect(functools.partial(self.init_segment_anything, model_name))
        #     action.setText("{}".format(model_name))
        #     action.setCheckable(True)
        #
        #     self.pths_actions[model_name] = action
        #     self.menuSAM_model.addAction(action)

    def translate(self, language='zh'):
        if language == 'zh':
            self.trans.load(os.path.join(ISAT_ROOT, 'ui/zh_CN'))
        else:
            self.trans.load(os.path.join(ISAT_ROOT, 'ui/en'))
        language_icon = QtGui.QIcon()
        icon_path = ":/icon/icons/中文_chinese.svg" if language == 'en' else ":/icon/icons/英文_english.svg"
        language_icon.addPixmap(QtGui.QPixmap(icon_path), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.actionLanguage.setIcon(language_icon)
        language_tool_tip = '中文' if language == 'en' else 'English'
        self.actionLanguage.setToolTip(language_tool_tip)
        language_status_tip = '切换中文界面' if language == 'en' else 'Switch to English'
        self.actionLanguage.setStatusTip(language_status_tip)

        _app = QtWidgets.QApplication.instance()
        _app.installTranslator(self.trans)
        self.retranslateUi(self)
        self.info_dock_widget.retranslateUi(self.info_dock_widget)
        self.annos_dock_widget.retranslateUi(self.annos_dock_widget)
        self.files_dock_widget.retranslateUi(self.files_dock_widget)
        self.category_edit_widget.retranslateUi(self.category_edit_widget)
        self.categories_dock_widget.retranslateUi(self.categories_dock_widget)
        self.category_setting_dialog.retranslateUi(self.category_setting_dialog)
        self.model_manager_dialog.retranslateUi(self.model_manager_dialog)
        self.about_dialog.retranslateUi(self.about_dialog)
        self.shortcut_dialog.retranslateUi(self.shortcut_dialog)
        self.Converter_dialog.retranslateUi(self.Converter_dialog)
        self.video2frames_dialog.retranslateUi(self.video2frames_dialog)
        self.auto_segment_dialog.retranslateUi(self.auto_segment_dialog)
        self.annos_validator_dialog.retranslateUi(self.annos_validator_dialog)
        self.setting_dialog.retranslateUi(self.setting_dialog)

        # 手动添加翻译 ------
        _translate = QtCore.QCoreApplication.translate
        self.image_saturation.setStatusTip(_translate("MainWindow", "Image saturation."))
        self.image_saturation.setToolTip(_translate("MainWindow", "Image saturation"))

        self.categories_dock_widget.pushButton_group_mode.setStatusTip(_translate("MainWindow", "Group id auto add 1 when add a new polygon."))
        self.modeState.setStatusTip(_translate('MainWindow', 'View mode.'))

        # -----------------

    def change_language(self):
        change_to_language = 'en' if self.cfg['software']['language'] == 'zh' else 'zh'
        self.translate(change_to_language)
        self.cfg['software']['language'] = change_to_language
        self.save_software_cfg()

    def reload_cfg(self):
        # 软件配置
        self.cfg = load_config(self.software_config_file)
        if self.cfg is None:
            self.cfg = {}
        software_cfg = self.cfg.get('software', {})
        self.cfg['software'] = software_cfg

        language = software_cfg.get('language', 'en')
        self.cfg['software']['language'] = language
        self.translate(language)

        auto_save = software_cfg.get('auto_save', False)
        self.cfg['software']['auto_save'] = auto_save
        self.setting_dialog.checkBox_auto_save.setChecked(auto_save)

        real_time_area = software_cfg.get('real_time_area', False)
        self.cfg['software']['real_time_area'] = real_time_area
        self.setting_dialog.checkBox_real_time_area.setChecked(real_time_area)

        contour_mode = software_cfg.get('contour_mode', 'max_only')
        self.cfg['software']['contour_mode'] = contour_mode
        self.change_contour_mode(contour_mode)

        mask_alpha = software_cfg.get('mask_alpha', 0.5)
        self.cfg['software']['mask_alpha'] = mask_alpha
        self.setting_dialog.horizontalSlider_mask_alpha.setValue(int(mask_alpha * 10))

        vertex_size = software_cfg.get('vertex_size', 1)
        self.cfg['software']['vertex_size'] = int(vertex_size)
        self.setting_dialog.horizontalSlider_vertex_size.setValue(int(vertex_size))

        show_prompt = software_cfg.get('show_prompt', False)
        self.cfg['software']['show_prompt'] = bool(show_prompt)
        self.setting_dialog.checkBox_show_prompt.setChecked(show_prompt)

        show_edge = software_cfg.get('show_edge', True)
        self.cfg['software']['show_edge'] = bool(show_edge)
        self.setting_dialog.checkBox_show_edge.setChecked(show_edge)

        use_polydp = software_cfg.get('use_polydp', True)
        self.cfg['software']['use_polydp'] = bool(use_polydp)
        self.setting_dialog.checkBox_approx_polygon.setChecked(use_polydp)

        invisible_polygon = software_cfg.get('create_mode_invisible_polygon', True)
        self.cfg['software']['create_mode_invisible_polygon'] = bool(invisible_polygon)
        self.setting_dialog.checkBox_polygon_invisible.setChecked(invisible_polygon)

        use_bfloat16 = software_cfg.get('use_bfloat16', False) if torch.cuda.is_available() else False
        self.cfg['software']['use_bfloat16'] = bool(use_bfloat16)
        self.setting_dialog.checkBox_use_bfloat16.setChecked(use_bfloat16)
        self.setting_dialog.checkBox_use_bfloat16.setEnabled(torch.cuda.is_available())
        self.model_manager_dialog.update_ui()

        # 类别
        self.cfg.update(load_config(self.config_file))
        label_dict_list = self.cfg.get('label', [])
        if len(label_dict_list) < 1 or label_dict_list[0].get('name', 'unknow') != '__background__':
            label_dict_list.insert(0, {'color': '#000000', 'name': '__background__'})

        d = {}
        for label_dict in label_dict_list:
            category = label_dict.get('name', 'unknow')
            color = label_dict.get('color', '#000000')
            d[category] = color
        self.category_color_dict = d

        self.categories_dock_widget.update_widget()

        if self.current_index is not None:
            self.show_image(self.current_index)

        # shortcut
        self.load_actions_shortcut(default=False)

    def set_saved_state(self, is_saved:bool):
        if not is_saved:
            if self.cfg['software']['auto_save']:
                self.save()
                is_saved = True
        self.saved = is_saved
        if self.files_list is not None and self.current_index is not None:

            if is_saved:
                self.setWindowTitle(self.current_label.label_path)
            else:
                self.setWindowTitle('*{}'.format(self.current_label.label_path))

    def open_dir(self):
        dir = QtWidgets.QFileDialog.getExistingDirectory(self)
        if not dir:
            return

        # 等待sam线程退出，并清空特征缓存
        if self.use_segment_anything:
            self.seganythread.wait()
            self.seganythread.results_dict.clear()
            try:
                self.segany_video.inference_state = {}
            except: pass

        self.files_list.clear()
        self.files_dock_widget.listWidget.clear()

        files = []
        suffixs = tuple(['{}'.format(fmt.data().decode('ascii').lower()) for fmt in QtGui.QImageReader.supportedImageFormats()])
        for f in os.listdir(dir):
            if f.lower().endswith(suffixs):
                # f = os.path.join(dir, f)
                files.append(f)
        files = sorted(files)
        self.files_list = files

        self.files_dock_widget.update_widget()

        self.current_index = 0

        self.image_root = dir
        self.actionImages_dir.setStatusTip("Image root: {}".format(self.image_root))

        self.label_root = dir
        self.actionLabel_dir.setStatusTip("Label root: {}".format(self.label_root))

        self.saved = True

        if os.path.exists(os.path.join(dir, 'isat.yaml')):
            # load setting yaml
            self.config_file = os.path.join(dir, 'isat.yaml')
            self.reload_cfg()

        self.show_image(self.current_index)

    def save_dir(self):
        dir = QtWidgets.QFileDialog.getExistingDirectory(self)
        if not dir:
            return

        self.label_root = dir
        self.actionLabel_dir.setStatusTip("Label root: {}".format(self.label_root))
        # load setting yaml
        if os.path.exists(os.path.join(dir, 'isat.yaml')):
            self.config_file = os.path.join(dir, 'isat.yaml')
            self.reload_cfg()
        # 刷新图片
        if self.current_index is not None:
            self.show_image(self.current_index)

    def save(self):
        if self.current_label is None:
            return
        self.current_label.objects.clear()
        for polygon in self.polygons:
            object = polygon.to_object()
            self.current_label.objects.append(object)

        self.current_label.note = self.info_dock_widget.lineEdit_note.text()
        self.current_label.save_annotation()
        # 保存标注文件的同时保存一份isat配置文件
        self.save_cfg(os.path.join(self.label_root, 'isat.yaml'))
        self.set_saved_state(True)

    def update_group_display(self):
        self.categories_dock_widget.lineEdit_currentGroup.setText(str(self.current_group))

    def show_image(self, index:int, zoomfit:bool=True):
        if not self.saved:
            result = QtWidgets.QMessageBox.question(self, 'Warning', 'Proceed without saved?', QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No, QtWidgets.QMessageBox.StandardButton.No)
            if result == QtWidgets.QMessageBox.StandardButton.No:
                return

        self.reset_action()
        self.scene.cancel_draw()
        self.scene.unload_image()
        self.annos_dock_widget.listWidget.clear()
        self.change_bit_map_to_label()
        #
        self.files_dock_widget.label_prev_state.setStyleSheet("background-color: {};".format('#999999'))
        self.files_dock_widget.label_current_state.setStyleSheet("background-color: {};".format('#999999'))
        self.files_dock_widget.label_next_state.setStyleSheet("background-color: {};".format('#999999'))

        self.current_label = None
        self.load_finished = False
        self.saved = True
        if not -1 < index < len(self.files_list):
            return
        try:
            file_path = os.path.join(self.image_root, self.files_list[index])
            image_data = Image.open(file_path)

            self.png_palette = image_data.getpalette()
            if self.png_palette is not None and file_path.endswith('.png'):
                self.statusbar.showMessage('This is a label file.')
                self.can_be_annotated = False
            else:
                self.can_be_annotated = True

            if self.can_be_annotated:
                self.actionPolygon.setEnabled(True)
                self.actionSave.setEnabled(True)
                self.actionBit_map.setEnabled(True)
                self.actionBackspace.setEnabled(True)
                self.actionFinish.setEnabled(True)
                self.actionCancel.setEnabled(True)
                self.actionVisible.setEnabled(True)
            else:
                self.actionPolygon.setEnabled(False)
                self.actionSave.setEnabled(False)
                self.actionBit_map.setEnabled(False)
                self.actionBackspace.setEnabled(False)
                self.actionFinish.setEnabled(False)
                self.actionCancel.setEnabled(False)
                self.actionVisible.setEnabled(False)

            self.scene.load_image(file_path)

            if self.use_segment_anything and self.can_be_annotated:
                self.segany.reset_image()
                self.seganythread.index = index
                self.seganythread.start()
                self.SeganyEnabled()
            if zoomfit:
                self.view.zoomfit()

            # load label
            if self.can_be_annotated:
                self.current_group = 1
                _, name = os.path.split(file_path)
                label_path = os.path.join(self.label_root, '.'.join(name.split('.')[:-1]) + '.json')
                self.current_label = Annotation(file_path, label_path)
                # 载入数据
                self.current_label.load_annotation()

                for object in self.current_label.objects:
                    try:
                        group = int(object.group)
                        # 新增 手动/自动 group选择
                        if self.group_select_mode == 'auto':
                            self.current_group = group + 1 if group >= self.current_group else self.current_group
                        elif self.group_select_mode == 'manual':
                            self.current_group = 1
                    except Exception as e:
                        pass
                    polygon = Polygon()
                    self.scene.addItem(polygon)
                    polygon.load_object(object)
                    self.polygons.append(polygon)
                self.update_group_display()
            if self.current_label is not None:
                self.setWindowTitle('{}'.format(self.current_label.label_path))
            else:
                self.setWindowTitle('{}'.format(file_path))

            self.annos_dock_widget.update_listwidget()
            self.info_dock_widget.update_widget()
            self.files_dock_widget.set_select(index)
            self.current_index = index
            self.files_dock_widget.label_current.setText('{}'.format(self.current_index+1))
            self.load_finished = True

        except Exception as e:
            print(e)
        finally:
            if self.current_index > 0:
                self.actionPrev_image.setEnabled(True)
            else:
                self.actionPrev_image.setEnabled(False)

            if self.current_index < len(self.files_list) - 1:
                self.actionNext_image.setEnabled(True)
            else:
                self.actionNext_image.setEnabled(False)

    # def prev_image(self):
    #     if self.scene.mode != STATUSMode.VIEW:
    #         return
    #     if self.current_index is None:
    #         return
    #     current_index = self.current_index - 1
    #     if current_index < 0:
    #         QtWidgets.QMessageBox.warning(self, 'Warning', 'This is the first picture.')
    #     else:
    #         self.show_image(current_index)

    # def next_image(self):
    #     if self.scene.mode != STATUSMode.VIEW:
    #         return
    #     if self.current_index is None:
    #         return
    #     current_index = self.current_index + 1
    #     if current_index > len(self.files_list) - 1:
    #         QtWidgets.QMessageBox.warning(self, 'Warning', 'This is the last picture.')
    #     else:
    #         self.show_image(current_index)
    # 修改next_image方法
    def next_image(self):
        if self.scene.mode != STATUSMode.VIEW:
            return
        if self.current_index is None:
            return

        # 新增过滤逻辑
        def find_next_annotated(start_idx):
            for i in range(start_idx + 1, len(self.files_list)):
                img_path = os.path.join(self.image_root, self.files_list[i])
                json_path = os.path.splitext(img_path)[0] + '.json'
                if os.path.exists(json_path):
                    return i
            return None

        current_index = self.current_index + 1
        if self.filter_annotated:
            found_idx = find_next_annotated(self.current_index)
            if found_idx is not None:
                self.show_image(found_idx)
            else:
                QtWidgets.QMessageBox.warning(self, 'Warning', 'No more annotated images.')
        else:
            if current_index > len(self.files_list) - 1:
                QtWidgets.QMessageBox.warning(self, 'Warning', 'This is the last picture.')
            else:
                self.show_image(current_index)

    # 修改prev_image方法（同理）
    def prev_image(self):
        if self.scene.mode != STATUSMode.VIEW:
            return
        if self.current_index is None:
            return

        # 新增过滤逻辑
        def find_prev_annotated(start_idx):
            for i in range(start_idx - 1, -1, -1):
                img_path = os.path.join(self.image_root, self.files_list[i])
                json_path = os.path.splitext(img_path)[0] + '.json'
                if os.path.exists(json_path):
                    return i
            return None

        current_index = self.current_index - 1
        if self.filter_annotated:
            found_idx = find_prev_annotated(self.current_index)
            if found_idx is not None:
                self.show_image(found_idx)
            else:
                QtWidgets.QMessageBox.warning(self, 'Warning', 'No previous annotated images.')
        else:
            if current_index < 0:
                QtWidgets.QMessageBox.warning(self, 'Warning', 'This is the first picture.')
            else:
                self.show_image(current_index)

    # 新增过滤模式切换方法
    # def toggle_filter_mode(self):
    #     self.filter_annotated = not self.filter_annotated
    #     state = "ON" if self.filter_annotated else "OFF"
    #     self.statusBar().showMessage(f"Annotation filter mode: {state}", 3000)
    #     print("filter_annotated:",self.filter_annotated)
    # def toggle_filter_mode(self, checked):
    #     self.filter_annotated = checked
    #     state = "ON" if checked else "OFF"
    #     self.statusBar().showMessage(f"标注过滤模式: {state}", 3000)
    def toggle_filter_mode(self, checked):
        #print("切换图片过滤翻页模式")
        """切换标注过滤模式"""
        self.filter_annotated = checked
        
        # 更新按钮状态显示
        self.actionToggleFilter.setIconText("图片过滤翻页: {}".format("开" if checked else "关"))
        
        # 状态栏提示（保持原有逻辑）
        state = "ON" if checked else "OFF"
        self.statusBar().showMessage(f"图片过滤翻页: {state}", 3000)
    
        
       

    def jump_to(self):
        index = self.files_dock_widget.lineEdit_jump.text()
        if index:
            if not index.isdigit():
                if index in self.files_list:
                    index = self.files_list.index(index)+1
                else:
                    QtWidgets.QMessageBox.warning(self, 'Warning', 'Don`t exist image named: {}'.format(index))
                    self.files_dock_widget.lineEdit_jump.clear()
                    return
            index = int(index)-1
            if 0 <= index < len(self.files_list):
                self.show_image(index)
                self.files_dock_widget.lineEdit_jump.clear()
            else:
                QtWidgets.QMessageBox.warning(self, 'Warning', 'Index must be in [1, {}].'.format(len(self.files_list)))
                self.files_dock_widget.lineEdit_jump.clear()
                self.files_dock_widget.lineEdit_jump.clearFocus()
                return

    def cancel_draw(self):
        self.scene.cancel_draw()

    def category_setting(self):
        self.category_setting_dialog.load_cfg()
        self.category_setting_dialog.show()

    def add_new_object(self, category, group, segmentation, area, layer, bbox):
        if self.current_label is None:
            return
        object = Object(category=category, group=group, segmentation=segmentation, area=area, layer=layer, bbox=bbox)
        self.current_label.objects.append(object)

    def delete_object(self, index:int):
        if 0 <= index < len(self.current_label.objects):
            del self.current_label.objects[index]

    def change_bit_map_to_semantic(self):
        # to semantic
        for polygon in self.polygons:
            polygon.setEnabled(False)
            for vertex in polygon.vertexs:
                vertex.setVisible(False)
            polygon.change_color(QtGui.QColor(self.category_color_dict.get(polygon.category, '#000000')))
            polygon.color.setAlpha(255)
            polygon.setBrush(polygon.color)
        self.annos_dock_widget.listWidget.setEnabled(False)
        self.annos_dock_widget.checkBox_visible.setEnabled(False)
        self.actionSegment_anything_point.setEnabled(False)
        self.actionSegment_anything_box.setEnabled(False)
        self.actionVideo_segment.setEnabled(False)
        self.actionVideo_segment_once.setEnabled(False)
        self.actionVideo_segment_five_times.setEnabled(False)
        self.actionPolygon.setEnabled(False)
        self.actionVisible.setEnabled(False)
        self.map_mode = MAPMode.SEMANTIC
        semantic_icon = QtGui.QIcon()
        semantic_icon.addPixmap(QtGui.QPixmap(":/icon/icons/semantic.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.actionBit_map.setIcon(semantic_icon)

    def change_bit_map_to_instance(self):
        # to instance
        for polygon in self.polygons:
            polygon.setEnabled(False)
            for vertex in polygon.vertexs:
                vertex.setVisible(False)
            if polygon.group != '':
                index = int(polygon.group)
                index = index % self.instance_cmap.shape[0]
                rgb = self.instance_cmap[index]
            else:
                rgb = self.instance_cmap[0]
            polygon.change_color(QtGui.QColor(rgb[0], rgb[1], rgb[2], 255))
            polygon.color.setAlpha(255)
            polygon.setBrush(polygon.color)
        self.annos_dock_widget.listWidget.setEnabled(False)
        self.annos_dock_widget.checkBox_visible.setEnabled(False)
        self.actionSegment_anything_point.setEnabled(False)
        self.actionSegment_anything_box.setEnabled(False)
        self.actionVideo_segment.setEnabled(False)
        self.actionVideo_segment_once.setEnabled(False)
        self.actionVideo_segment_five_times.setEnabled(False)
        self.actionPolygon.setEnabled(False)
        self.actionVisible.setEnabled(False)
        self.map_mode = MAPMode.INSTANCE
        instance_icon = QtGui.QIcon()
        instance_icon.addPixmap(QtGui.QPixmap(":/icon/icons/instance.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.actionBit_map.setIcon(instance_icon)

    def change_bit_map_to_label(self):
        # to label
        for polygon in self.polygons:
            polygon.setEnabled(True)
            for vertex in polygon.vertexs:
                # vertex.setEnabled(True)
                vertex.setVisible(polygon.isVisible())
            polygon.change_color(QtGui.QColor(self.category_color_dict.get(polygon.category, '#000000')))
            polygon.color.setAlpha(polygon.nohover_alpha)
            polygon.setBrush(polygon.color)
        self.annos_dock_widget.listWidget.setEnabled(True)
        self.annos_dock_widget.checkBox_visible.setEnabled(True)
        self.SeganyEnabled()
        self.actionVideo_segment.setEnabled(self.use_segment_anything_video)
        self.actionVideo_segment_once.setEnabled(self.use_segment_anything_video)
        self.actionVideo_segment_five_times.setEnabled(self.use_segment_anything_video)
        self.actionPolygon.setEnabled(True)
        self.actionVisible.setEnabled(True)
        self.map_mode = MAPMode.LABEL
        label_icon = QtGui.QIcon()
        label_icon.addPixmap(QtGui.QPixmap(":/icon/icons/照片_pic.svg"), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.actionBit_map.setIcon(label_icon)

    def change_bit_map(self):
        self.set_labels_visible(True)
        if self.scene.mode == STATUSMode.CREATE:
            self.scene.cancel_draw()
        if self.map_mode == MAPMode.LABEL:
            self.change_bit_map_to_semantic()

        elif self.map_mode == MAPMode.SEMANTIC:
            self.change_bit_map_to_instance()

        elif self.map_mode == MAPMode.INSTANCE:
            self.change_bit_map_to_label()
        else:
            pass

    def set_labels_visible(self, visible=None):
        if visible is None:
            visible = not self.annos_dock_widget.checkBox_visible.isChecked()
        self.annos_dock_widget.set_all_polygon_visible(visible)

    def model_manage(self):
        self.model_manager_dialog.show()

    def change_bfloat16_state(self, check_state):
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['use_bfloat16'] = checked
        self.init_segment_anything()
        self.model_manager_dialog.update_ui()
        self.save_software_cfg()

    def change_contour_mode(self, contour_mode='max_only'):
        if contour_mode == 'max_only':
            self.scene.change_contour_mode_to_save_max_only()
        elif contour_mode == 'external':
            self.scene.change_contour_mode_to_save_external()
            self.statusbar.showMessage('Save all external contours will bring some noise.', 3000)
        elif contour_mode == 'all':
            self.scene.change_contour_mode_to_save_all()
            self.statusbar.showMessage('Category of inner contour will be set _background__.', 3000)
        else:
            self.scene.change_contour_mode_to_save_max_only()
            self.statusbar.showMessage('The contour mode [{}] not support.'.format(contour_mode), 3000)

        if contour_mode == 'external':
            index = 0
        elif contour_mode == 'max_only':
            index = 1
        elif contour_mode == 'all':
            index = 2
        else:
            index = 0
        self.setting_dialog.comboBox_contour_mode.setCurrentIndex(index)
        self.cfg['software']['contour_mode'] = contour_mode
        self.save_software_cfg()

    def change_mask_aplha(self, value):
        value = value / 10
        self.scene.mask_alpha = value
        self.scene.update_mask()
        self.cfg['software']['mask_alpha'] = value
        self.save_software_cfg()
        self.setting_dialog.label_mask_alpha.setText('{}'.format(value))

    def change_vertex_size(self, value):
        self.cfg['software']['vertex_size'] = value
        self.save_software_cfg()
        if self.current_index is not None:
            self.show_image(self.current_index, zoomfit=False)
        self.setting_dialog.label_vertex_size.setText('{}'.format(value))

    def change_auto_save_state(self, check_state):
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['auto_save'] = checked
        self.save_software_cfg()

    def change_real_time_area_state(self, check_state):
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['real_time_area'] = checked
        self.save_software_cfg()

    def change_edge_state(self, check_state):
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['show_edge'] = checked
        self.save_software_cfg()
        if self.current_index is not None:
            self.show_image(self.current_index, zoomfit=False)

    def change_approx_polygon_state(self, check_state):  # 是否使用多边形拟合，来减少多边形顶点
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['use_polydp'] = checked
        self.save_software_cfg()
        if self.current_index is not None:
            self.show_image(self.current_index, zoomfit=False)

    def change_create_mode_invisible_polygon_state(self, check_state):
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['create_mode_invisible_polygon'] = checked
        self.save_software_cfg()
        if self.current_index is not None:
            self.show_image(self.current_index, zoomfit=False)


        pass
    def change_saturation(self, value):  # 调整图像饱和度
        if self.scene.image_data is not None:
            saturation_scale = value / 100.0
            hsv_image = cv2.cvtColor(self.scene.image_data, cv2.COLOR_RGB2HSV)
            hsv_image[:, :, 1] = np.clip(hsv_image[:, :, 1] * saturation_scale, 0, 255)
            image_hsv = cv2.cvtColor(hsv_image, cv2.COLOR_HSV2RGB)
            height, width, channels = self.scene.image_data.shape
            pixmap = QtGui.QPixmap.fromImage(QtGui.QImage(image_hsv.data, width, height, channels * width, QtGui.QImage.Format_RGB888))
            self.scene.image_item.setPixmap(pixmap)
        else:
            print('Image data not loaded in AnnotationScene')

    def change_prompt_visiable(self, check_state):
        checked = check_state == QtCore.Qt.CheckState.Checked
        self.cfg['software']['show_prompt'] = checked
        self.save_software_cfg()
        for item in self.scene.items():
            if isinstance(item, PromptPoint):
                item.setVisible(checked)
        # if self.current_index is not None:
        #     self.show_image(self.current_index)

    def converter(self):
        current_converter = self.cfg['software'].get('current_converter', 'coco')
        if current_converter == 'coco':
            current_converter_tab = self.Converter_dialog.tab_COCO
        elif current_converter == 'yolo':
            current_converter_tab = self.Converter_dialog.tab_YOLO
        elif current_converter == 'labelme':
            current_converter_tab = self.Converter_dialog.tab_LABELME
        elif current_converter == 'voc':
            current_converter_tab = self.Converter_dialog.tab_VOC
        elif current_converter == 'voc for detection':
            current_converter_tab = self.Converter_dialog.tab_VOC_DETECTION
        else:
            current_converter_tab = self.Converter_dialog.tab_COCO

        self.Converter_dialog.tabWidget.setCurrentWidget(current_converter_tab)

        self.Converter_dialog.show()

    def video2frames(self):
        self.video2frames_dialog.show()

    def auto_segment(self):
        if self.use_segment_anything:
            self.auto_segment_dialog.show()
        else:
            QtWidgets.QMessageBox.warning(self, 'Warning', 'Select a sam model before auto segment.')

    def annos_validator(self):
        self.annos_validator_dialog.show()

    def shortcut(self):
        self.shortcut_dialog.update_ui()
        self.shortcut_dialog.show()

    def about(self):
        self.about_dialog.show()

    def setting(self):
        # self.setting_dialog.update_ui()
        self.setting_dialog.show()

    def screen_shot(self, type='scene'):
        # image_name = "ISAT-{}-{}.png".format(type, datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        if self.current_index is not None and self.files_list:
            current_image = self.files_list[self.current_index]
        else:
            current_image = 'no_image.jpg'
            if type == 'scene':
                print('no image saved.')
                return
        image_name = os.path.splitext(current_image)[0] + '.png'
        screen_shot_dir = os.path.join(os.getcwd(), 'screen_shots')
        os.makedirs(screen_shot_dir, exist_ok=True)
        save_path = os.path.join(screen_shot_dir, image_name)
        print(f'save path: {save_path}')

        if type == 'scene':
            try:
                self.scene.guide_line_x.setVisible(False)
                self.scene.guide_line_y.setVisible(False)
            except: pass
            image = QtGui.QImage(self.scene.sceneRect().size().toSize(), QtGui.QImage.Format_ARGB32)
            painter = QtGui.QPainter(image)
            self.scene.render(painter)
            painter.end()
            image.save(save_path)
            self.statusbar.showMessage('Save scene screenshot to {}'.format(save_path), 3000)
            try:
                self.scene.guide_line_x.setVisible(True)
                self.scene.guide_line_y.setVisible(True)
            except: pass
        elif type == 'window':
            image = QtGui.QImage(self.size(), QtGui.QImage.Format_ARGB32)
            painter = QtGui.QPainter(image)
            self.render(painter)
            painter.end()
            image.save(save_path)
            self.statusbar.showMessage('Save window screenshot to {}'.format(save_path), 3000)

        else:
            return

    def save_cfg(self, config_file):
        # 只保存类别配置
        cfg = {'label': self.cfg.get('label', [])}
        save_config(cfg, config_file)

    def save_software_cfg(self):
        save_config(self.cfg, self.software_config_file)

    def exit(self):
        # 保存类别配置
        self.save_cfg(self.config_file)
        # 保存软件配置
        self.save_software_cfg()
        self.close()

    def closeEvent(self, a0: QtGui.QCloseEvent):
        self.exit()

    def init_connect(self):
        self.actionImages_dir.triggered.connect(self.open_dir)
        self.actionLabel_dir.triggered.connect(self.save_dir)
        self.actionVideo_segment.triggered.connect(functools.partial(self.seg_video_start, None))
        self.actionVideo_segment_once.triggered.connect(functools.partial(self.seg_video_start, 1))
        self.actionVideo_segment_five_times.triggered.connect(functools.partial(self.seg_video_start, 5))

        self.actionPrev_image.triggered.connect(self.prev_image)
        self.actionNext_image.triggered.connect(self.next_image)
        self.actionSetting.triggered.connect(self.setting)
        self.actionExit.triggered.connect(self.exit)

        self.actionSegment_anything_point.triggered.connect(self.scene.start_segment_anything)
        self.actionSegment_anything_box.triggered.connect(self.scene.start_segment_anything_box)
        self.actionPolygon.triggered.connect(self.scene.start_draw_polygon)
        self.actionRepaint.triggered.connect(self.scene.change_mode_to_repaint)
        self.actionCancel.triggered.connect(self.scene.cancel_draw)
        self.actionBackspace.triggered.connect(self.scene.backspace)
        self.actionFinish.triggered.connect(self.scene.finish_draw)
        self.actionEdit.triggered.connect(self.scene.edit_polygon)
        self.actionDelete.triggered.connect(self.scene.delete_selected_graph)
        self.actionSave.triggered.connect(self.save)
        self.actionTo_top.triggered.connect(self.scene.move_polygon_to_top)
        self.actionTo_bottom.triggered.connect(self.scene.move_polygon_to_bottom)
        self.actionCopy.triggered.connect(self.scene.copy_item)
        self.actionUnion.triggered.connect(self.scene.polygons_union)
        self.actionSubtract.triggered.connect(self.scene.polygons_difference)
        self.actionIntersect.triggered.connect(self.scene.polygons_intersection)
        self.actionExclude.triggered.connect(self.scene.polygons_symmetric_difference)

        self.actionPrev_group.triggered.connect(self.annos_dock_widget.go_to_prev_group)
        self.actionNext_group.triggered.connect(self.annos_dock_widget.go_to_next_group)
        self.actionZoom_in.triggered.connect(self.view.zoom_in)
        self.actionZoom_out.triggered.connect(self.view.zoom_out)
        self.actionFit_window.triggered.connect(self.view.zoomfit)
        self.actionBit_map.triggered.connect(self.change_bit_map)
        self.actionVisible.triggered.connect(functools.partial(self.set_labels_visible, None))
        self.actionScene_shot.triggered.connect(functools.partial(self.screen_shot, 'scene'))
        self.actionWindow_shot.triggered.connect(functools.partial(self.screen_shot, 'window'))

        self.actionModel_manage.triggered.connect(self.model_manage)
        self.actionModel_manage.setStatusTip(CHECKPOINT_PATH)

        self.actionConverter.triggered.connect(self.converter)
        self.actionVideo_to_frames.triggered.connect(self.video2frames)
        self.actionAuto_segment_with_bounding_box.triggered.connect(self.auto_segment)
        self.actionAnno_validator.triggered.connect(self.annos_validator)

        self.actionShortcut.triggered.connect(self.shortcut)
        self.actionAbout.triggered.connect(self.about)

        self.actionLanguage.triggered.connect(self.change_language)

        self.annos_dock_widget.listWidget.doubleClicked.connect(self.scene.edit_polygon)

    def reset_action(self):
        self.actionPrev_image.setEnabled(False)
        self.actionNext_image.setEnabled(False)
        self.actionSegment_anything_point.setEnabled(False)
        self.actionSegment_anything_box.setEnabled(False)
        self.actionPolygon.setEnabled(False)
        self.actionRepaint.setEnabled(False)
        self.actionVideo_segment.setEnabled(False)
        self.actionVideo_segment_once.setEnabled(False)
        self.actionVideo_segment_five_times.setEnabled(False)
        self.actionEdit.setEnabled(False)
        self.actionDelete.setEnabled(False)
        self.actionSave.setEnabled(False)
        self.actionTo_top.setEnabled(False)
        self.actionTo_bottom.setEnabled(False)
        self.actionBit_map.setChecked(False)
        self.actionBit_map.setEnabled(False)
        self.actionCopy.setEnabled(False)
        self.actionUnion.setEnabled(False)
        self.actionSubtract.setEnabled(False)
        self.actionIntersect.setEnabled(False)
        self.actionExclude.setEnabled(False)

    def load_actions_shortcut(self, default=False):
        shortcut_cfg = self.cfg['shortcut']
        for action in shortcut_cfg:

            default_key = shortcut_cfg[action]['default_key']
            if default:
                shortcut_cfg[action]['key'] = default_key
            key = shortcut_cfg[action]['key']
            try:
                if key is not None:
                    eval('self.' + action).setShortcut(QtGui.QKeySequence(key))
                else:
                    eval('self.' + action).setShortcut(QtGui.QKeySequence(0))
            except Exception as e:
                pass

    def latest_version_tip(self, is_latest_version, latest_version):
        if not is_latest_version:
            if self.cfg['software']['language'] == 'zh':
                title = ''
                text = f'<html><head/><body><p align=\"center\">新版本<b>{latest_version}</b>已发布!</p><p align=\"center\">请在<a href=\"https://github.com/yatengLG/ISAT_with_segment_anything/releases\">Github</a>上查看更新内容</p></body></html>'
            else:
                title = ''
                text = f'<html><head/><body><p align=\"center\">New version <b>{latest_version}</b> released!</p><p align=\"center\">Check the update content on <a href=\"https://github.com/yatengLG/ISAT_with_segment_anything/releases\">Github</a></p></body></html>'

            QtWidgets.QMessageBox.information(self, title, text)
