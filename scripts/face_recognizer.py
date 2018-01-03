#!/usr/bin/env python2
#
# Copyright 2015-2016 Carnegie Mellon University
# Copyright 2016 Hanson Robotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import cv2
import pickle
import random
import uuid
import datetime as dt
import time
import numpy as np
import pandas as pd
import logging
import threading
import shutil
import tempfile
from collections import deque

from sklearn.preprocessing import LabelEncoder
from sklearn.svm import SVC
import dlib
import openface
from openface.data import iterImgs
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image
from dynamic_reconfigure.server import Server
import dynamic_reconfigure.client
from ros_face_recognition.cfg import FaceRecognitionConfig
from ros_face_recognition.utils import get_3d_point
from ros_face_recognition.msg import Face, Faces
from std_msgs.msg import String

CWD = os.path.dirname(os.path.abspath(__file__))
HR_MODELS = os.environ.get('HR_MODELS', os.path.expanduser('~/.hr/models'))
DATA_DIR = os.path.join(os.path.expanduser('~/.hr/data'), 'faces')
DATA_ARCHIVE_DIR = os.path.join(DATA_DIR, 'archive')
CLASSIFIER_DIR = os.path.join(DATA_DIR, 'classifier')
DEFAULT_CLASSIFIER_DIR = os.path.join(HR_MODELS, 'classifier')
DLIB_FACEPREDICTOR = os.path.join(HR_MODELS,
                    'shape_predictor_68_face_landmarks.dat')
NETWORK_MODEL = os.path.join(HR_MODELS, 'nn4.small2.v1.t7')
logger = logging.getLogger('hr.vision.ros_face_recognition.face_recognizer')

for d in [DATA_DIR, DATA_ARCHIVE_DIR, CLASSIFIER_DIR]:
    if not os.path.isdir(d):
        os.makedirs(d)

class FaceRecognizer(object):

    class Face(object):
        def __init__(self, name, confidence, bbox, landmarks):
            self.name = name
            self.confidence = confidence
            self.bbox = bbox
            self.landmarks = landmarks

    def __init__(self):
        self.bridge = CvBridge()
        self.imgDim = 96
        self.align = openface.AlignDlib(DLIB_FACEPREDICTOR)
        self.face_pose_predictor = dlib.shape_predictor(DLIB_FACEPREDICTOR)
        self.net = openface.TorchNeuralNet(NETWORK_MODEL, self.imgDim)
        self.landmarkIndices = openface.AlignDlib.OUTER_EYES_AND_NOSE
        self.face_detector = dlib.get_frontal_face_detector()
        self.count = 0
        self.face_count = 0 # Cumulative total faces in training.
        self.max_face_count = 10
        self.train = False
        self.enable = True
        self.train_dir = os.path.join(DATA_DIR, 'training-images')
        self.aligned_dir = os.path.join(DATA_DIR, 'aligned-images')
        self.clf, self.le = None, None
        self.known_names = rospy.get_param('known_names', [])
        classifier = os.path.join(CLASSIFIER_DIR, 'classifier.pkl')
        if os.path.isfile(classifier):
            self.load_classifier(classifier)
            label_fname = "{}/local_labels.csv".format(CLASSIFIER_DIR)
            if os.path.isfile(label_fname):
                df = pd.read_csv(label_fname, header=None)
                if not df.empty:
                    for name in set(df[0].tolist()):
                        self.known_names.append(name)
        else:
            self.load_classifier(os.path.join(DEFAULT_CLASSIFIER_DIR, 'classifier.pkl'))
        self.node_name = rospy.get_name()
        self.multi_faces = False
        self.threshold = 0.5
        self.detected_faces = deque(maxlen=10)
        self.training_job = None
        self.stop_training = threading.Event()
        self.faces = []
        self.event_pub = rospy.Publisher(
            'face_training_event', String, latch=True, queue_size=1)
        self.faces_pub = rospy.Publisher(
            '~faces', Faces, latch=True, queue_size=1)
        self.imgpub = rospy.Publisher(
            '~image', Image, latch=True, queue_size=1)
        self._lock = threading.RLock()
        self.colors = [ (255, 0, 0), (0, 255, 0), (0, 0, 255),
            (255, 255, 0), (255, 0, 255), (0, 255, 255) ]

    def load_classifier(self, model):
        if os.path.isfile(model):
            with open(model) as f:
                try:
                    self.le, self.clf = pickle.load(f)
                    logger.info("Loaded model {}".format(model))
                except Exception as ex:
                    logger.error("Loading model {} failed".format(model))
                    logger.error(ex)
                    self.clf, self.le = None, None
        else:
            logger.error("Model file {} is not found".format(model))

    def getRep(self, bgrImg, all=True):
        if bgrImg is None:
            return [], []

        rgbImg = cv2.cvtColor(bgrImg, cv2.COLOR_BGR2RGB)
        if all:
            bb = self.align.getAllFaceBoundingBoxes(rgbImg)
        else:
            bb = self.align.getLargestFaceBoundingBox(rgbImg)

        if bb is None:
            return [], []

        if not hasattr(bb, '__iter__'):
            bb = [bb]

        reps = []
        for box in bb:
            aligned_face = self.align.align(self.imgDim, rgbImg, box,
                    landmarkIndices=self.landmarkIndices)
            reps.append(self.net.forward(aligned_face))

        return reps, bb

    def align_image(self, imgObject, imgName):
        rgb = imgObject.getRGB()
        if rgb is None:
            outRgb = None
        else:
            outRgb = self.align.align(
                    self.imgDim, rgb,
                    landmarkIndices=self.landmarkIndices)
        if outRgb is not None:
            outBgr = cv2.cvtColor(outRgb, cv2.COLOR_RGB2BGR)
            cv2.imwrite(imgName, outBgr)
            logger.info("Write image {}".format(imgName))
            return True
        else:
            os.remove(imgObject.path)
            logger.warn("No face was detected in {}. Removed.".format(imgObject.path))
            return False

    def align_images(self, input_dir):
        imgs = list(iterImgs(input_dir))
        # Shuffle so multiple versions can be run at once.
        random.shuffle(imgs)

        for imgObject in imgs:
            outDir = os.path.join(self.aligned_dir, imgObject.cls)
            if not os.path.isdir(outDir):
                os.makedirs(outDir)
            outputPrefix = os.path.join(outDir, imgObject.name)
            imgName = outputPrefix + ".png"
            if not os.path.isfile(imgName):
                logger.info("Aligning image %s", imgName)
                try:
                    self.align_image(imgObject, imgName)
                except Exception as ex:
                    logger.error(ex)
            else:
                logger.info("Skip existing aligned image %s", imgName)

    def gen_data(self):
        face_reps = []
        labels = []
        reps_fname = "{}/reps.csv".format(CLASSIFIER_DIR)
        label_fname = "{}/labels.csv".format(CLASSIFIER_DIR)
        local_reps_fname = "{}/local_reps.csv".format(CLASSIFIER_DIR)
        local_label_fname = "{}/local_labels.csv".format(CLASSIFIER_DIR)
        for imgObject in iterImgs(self.aligned_dir):
            reps = self.net.forward(imgObject.getRGB())
            face_reps.append(reps)
            labels.append((imgObject.cls, imgObject.name))
        if face_reps and labels and not self.stop_training.is_set():
            pd.DataFrame(face_reps).to_csv(reps_fname, header=False, index=False)
            pd.DataFrame(labels).to_csv(label_fname, header=False, index=False)
            pd.DataFrame(face_reps).to_csv(local_reps_fname, header=False, index=False)
            pd.DataFrame(labels).to_csv(local_label_fname, header=False, index=False)
            logger.info("Generated label file {}".format(label_fname))
            logger.info("Generated representation file {}".format(reps_fname))

    def collect_face(self, image, crop=False):
        img_dir = os.path.join(self.train_dir, self.face_name)
        if not os.path.isdir(img_dir):
            os.makedirs(img_dir)
        detected_faces = self.face_detector(image)
        if detected_faces:
            face = max(detected_faces, key=lambda rect: rect.width() * rect.height())
            self.faces = [FaceRecognizer.Face('sample',1,face,None)]
            self.republish(image, self.faces)
            if crop:
                image = image[face.top():face.bottom(), face.left():face.right()]
                if image.size == 0:
                    return
            fname = os.path.join(img_dir, "{}.jpg".format(uuid.uuid1().hex))
            cv2.imwrite(fname, image)
            logger.info("Write face image to {}".format(fname))
            self.face_count += 1
            self.event_pub.publish('{}/{}'.format(self.face_count, self.max_face_count))

    def prepare(self):
        """Align faces, generate representations and labels"""
        logger.info("Preparing")
        self.align_images(self.train_dir)
        self.gen_data()

    def train_model(self):
        with self._lock:
            name = self.face_name
            logger.info("Training model")
            self.event_pub.publish('training')
            self.prepare()
            label_fname = "{}/labels.csv".format(CLASSIFIER_DIR)
            reps_fname = "{}/reps.csv".format(CLASSIFIER_DIR)
            labels, embeddings = None, None
            if os.path.isfile(label_fname) and \
                        os.path.isfile(reps_fname):
                labels = pd.read_csv(label_fname, header=None)
                embeddings = pd.read_csv(reps_fname, header=None)

            if labels is None or embeddings is None:
                logger.error("No labels or representations are found")
                self.event_pub.publish('abort')
                return

            # append the existing data
            original_label_fname = "{}/labels.csv".format(DEFAULT_CLASSIFIER_DIR)
            original_reps_fname = "{}/reps.csv".format(DEFAULT_CLASSIFIER_DIR)
            if os.path.isfile(original_label_fname) and \
                        os.path.isfile(original_reps_fname):
                labels2 = pd.read_csv(original_label_fname, header=None)
                embeddings2 = pd.read_csv(original_reps_fname, header=None)
                labels = labels.append(labels2)
                embeddings = embeddings.append(embeddings2)

            labels_data = labels.as_matrix()[:,0].tolist()
            embeddings_data = embeddings.as_matrix()

            le = LabelEncoder().fit(labels_data)
            labelsNum = le.transform(labels_data)
            clf = SVC(C=1, kernel='linear', probability=True)
            try:
                clf.fit(embeddings_data, labelsNum)
            except ValueError as ex:
                logger.error(ex)
                self.event_pub.publish('abort')
                return

            if not self.stop_training.is_set():
                labels.to_csv(label_fname, header=False, index=False)
                embeddings.to_csv(reps_fname, header=False, index=False)
                logger.info("Update label file {}".format(label_fname))
                logger.info("Update representation file {}".format(reps_fname))

                classifier_fname = "{}/classifier.pkl".format(CLASSIFIER_DIR)
                with open(classifier_fname, 'w') as f:
                    pickle.dump((le, clf), f)
                logger.info("Model saved to {}".format(classifier_fname))

                self.load_classifier(classifier_fname)
                self.known_names.append(self.face_name)
                self.event_pub.publish('end')
            else:
                self.event_pub.publish('abort')

    def infer(self, img):
        if self.clf is None or self.le is None:
            return None, None, None
        reps, bb = self.getRep(img, self.multi_faces)
        persons = []
        confidences = []
        bboxes = []
        for rep, box in zip(reps, bb):
            try:
                rep = rep.reshape(1, -1)
            except:
                logger.info("No Face detected")
                return None, None, None
            predictions = self.clf.predict_proba(rep).ravel()
            maxI = np.argmax(predictions)
            label = self.le.inverse_transform(maxI)
            if label not in self.known_names:
                logger.info("{} is not in known names".format(label))
                continue
            persons.append(self.le.inverse_transform(maxI))
            confidences.append(predictions[maxI])
            bboxes.append(box)
        return persons, confidences, bboxes

    def overlay_image(self, image, faces):
        i = 0
        for face in sorted(faces,
                key=lambda x: x.bbox.width()*x.bbox.height(), reverse=True):
            b = face.bbox
            p = face.name
            cv2.rectangle(image, (b.left(), b.top()), (b.right(), b.bottom()), self.colors[i], 2)
            cv2.putText(image, p, (b.left(), b.top()-10), cv2.FONT_HERSHEY_SIMPLEX, 1, self.colors[i], 2)
            landmarks = face.landmarks
            if landmarks:
                for j in range(landmarks.num_parts):
                    point = landmarks.part(j)
                    x = int(point.x)
                    y = int(point.y)
                    cv2.circle(image, (x,y), 1, self.colors[i], 1)
            i += 1
            i = i%6

    def republish(self, image, faces):
        if isinstance(image, Image):
            image = self.bridge.imgmsg_to_cv2(image, "bgr8")
        self.overlay_image(image, faces)
        self.imgpub.publish(self.bridge.cv2_to_imgmsg(image, 'bgr8'))

    def image_cb(self, ros_image):
        if not self.enable:
            return

        self.count += 1
        if self.count % 30 != 0:
            self.republish(ros_image, self.faces)
            return
        image = self.bridge.imgmsg_to_cv2(ros_image, "bgr8")
        if self.train:
            self.collect_face(image)
            if self.face_count == self.max_face_count:
                try:
                    self.faces = []
                    self.training_job = threading.Thread(target=self.train_model)
                    self.training_job.deamon = True
                    self.training_job.start()
                    while not self.stop_training.is_set() and self.training_job.is_alive():
                        self.training_job.join(0.2)
                    if self.training_job.is_alive():
                        logger.info("Training is interrupted")
                    else:
                        logger.info("Training model is finished")
                except Exception as ex:
                    logger.error("Train model failed")
                    logger.error(ex)
                finally:
                    self.training_job = None
                    self.train = False
                    self.update_parameter({'train': False})
                    self.update_parameter({'face_name': ''})
                    self.face_count = 0
        else:
            persons, confidences, bboxes = self.infer(image)
            if persons:
                faces = []
                for p, c, b in zip(persons, confidences, bboxes):
                    l = self.face_pose_predictor(image, b)
                    faces.append(FaceRecognizer.Face(p,c,b,l))
                    logger.info("P: {} C: {}".format(p, c))
                faces = sorted(faces,
                        key=lambda x: x.bbox.width()*x.bbox.height(), reverse=True)
                self.faces = faces
                current = '|'.join([f.name for f in self.faces if f.confidence > self.threshold])
                self.detected_faces.append(current)
                rospy.set_param('{}/recent_persons'.format(self.node_name),
                            ','.join(self.detected_faces))
                rospy.set_param('{}/current_persons'.format(self.node_name),
                            current)
                rospy.set_param('{}/face_visible'.format(self.node_name), True)
            else:
                if self.count % 150 == 0: # wait ~5 seconds to let it pick up the face again
                    self.faces = []
                    rospy.set_param('{}/face_visible'.format(self.node_name), False)
                    rospy.set_param('{}/current_persons'.format(self.node_name),'')
            self.publish_faces(self.faces)
        self.republish(ros_image, self.faces)

    def publish_faces(self, faces):
        msgs = Faces()
        for face in faces:
            msg = Face()
            msg.faceid = face.name
            msg.left = face.bbox.left()
            msg.top = face.bbox.top()
            msg.right = face.bbox.right()
            msg.bottom = face.bbox.bottom()
            msg.confidence = face.confidence
            msgs.faces.append(msg)
        self.faces_pub.publish(msgs)

    def archive(self):
        archive_fname = os.path.join(DATA_ARCHIVE_DIR, 'faces-{}'.format(
                dt.datetime.strftime(dt.datetime.now(), '%Y%m%d%H%M%S')))
        shutil.make_archive(archive_fname, 'gztar', root_dir=DATA_DIR)

    def reset(self):
        shutil.rmtree(self.train_dir, ignore_errors=True)
        shutil.rmtree(self.aligned_dir, ignore_errors=True)
        shutil.rmtree(os.path.join(CLASSIFIER_DIR, 'classifier.pkl'), ignore_errors=True)
        self.load_classifier(os.path.join(DEFAULT_CLASSIFIER_DIR, 'classifier.pkl'))
        logger.warn("Model is reset to default")

    def save_model(self):
        files = ['labels.csv', 'reps.csv', 'classifier.pkl']
        files = [os.path.join(self.aligned_dir, f) for f in files]
        if all([os.path.isfile(f) for f in files]):
            for f in files:
                shutil.copy(f, os.path.join(CLASSIFIER_DIR))
            logger.info("Model is saved")
            self.archive()
            return True
        logger.info("Model is not saved")
        return False

    def update_parameter(self, param):
        client = dynamic_reconfigure.client.Client(self.node_name, timeout=2)
        try:
            client.update_configuration(param)
        except Exception as ex:
            logger.error("Updating parameter error: {}".format(ex))
            return False
        return True

    def reconfig(self, config, level):
        self.enable = config.enable
        if not self.enable:
            config.reset = False
            config.train = False
            return config
        if config.save:
            self.save_model()
            config.save = False
        if self.train and not config.train:
            # TODO: stop training if it's started
            logger.info("Stopping")
            self.train = False
            self.stop_training.set()
            self.event_pub.publish('abort')
        self.face_name = config.face_name
        self.train = config.train
        if self.train:
            if self.face_name:
                self.face_name = self.face_name.lower()
                self.event_pub.publish('start')
                self.stop_training.clear()
                self.face_count = 0
            else:
                self.train = False
                config.train = False
                logger.error("Name is not set")
        self.threshold = config.confidence_threshold
        self.multi_faces = config.multi_faces
        self.max_face_count = config.max_face_count
        if config.reset:
            config.train = False
            self.train = False
            time.sleep(0.2)
            try:
                self.reset()
            except Exception as ex:
                logger.error(ex)
            config.reset = False
        return config

if __name__ == '__main__':
    rospy.init_node("face_recognizer")
    recognizer = FaceRecognizer()
    Server(FaceRecognitionConfig, recognizer.reconfig)
    rospy.Subscriber('/camera/image_raw', Image, recognizer.image_cb)
    rospy.spin()

    #logging.basicConfig()
    #logging.getLogger().setLevel(logging.INFO)
    #recognizer = FaceRecognizer()
    #recognizer.train_model()
    #
