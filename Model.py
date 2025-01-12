import numpy as np
import pandas as pd

from collections import Counter
from tqdm import tqdm

from sklearn.linear_model import LogisticRegression
from catboost import CatBoostClassifier
from sktime.transformations.panel.rocket import MiniRocket
from sklearn.linear_model import RidgeClassifierCV

from sklearn.compose import ColumnTransformer
from sklearn.model_selection import train_test_split, GridSearchCV, cross_validate
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.metrics import roc_auc_score

import warnings
warnings.filterwarnings("ignore")

import datetime 
from math import cos, asin, sqrt, pi

# Haversine formula for calculating distances between two points
def get_distance(lat1, lon1, lat2, lon2):
    r = 6371
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    delta_phi = np.radians(lat2 - lat1)
    delta_lambda = np.radians(lon2 - lon1)
    a = np.sin(delta_phi / 2)**2 + np.cos(phi1) * np.cos(phi2) *   np.sin(delta_lambda / 2)**2
    res = r * (2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a)))
    return np.round(res, 4)
    

def get_speed(lat1, lon1, lat2, lon2, dt1: str, dt2: str) -> float:
    distance = get_distance(lat1, lon1, lat2, lon2).tolist()
    format = "%Y-%m-%d %H:%M:%S"
    dt1=datetime.datetime.strptime(dt1, format)
    dt2=datetime.datetime.strptime(dt2, format)
    time = (dt2-dt1).total_seconds()/3600 # convert timedelta into hours
    if time==0:
        return 0
    return distance/time


class Model:
    def __init__(self):        
        self.model = None
        self.model_tracks = None
        self.model_rocket = None

        self.counter_words = {}

        self.TRACKS_CHUNK_SIZE = 20
        self.TRACKS_MULTIPLIER = 1
    
    def count_words(self, x):
        return len(x.split(" "))

    def check_sentence(self, sentence, words_type):
        words_count = 0
        for word in sentence.split(" "):
            word = word.lower().replace(',', '').replace('.', '')

            if (word not in list(self.counter_words.keys()) or len(self.counter_words[word]) == 2): continue

            if (words_type == self.counter_words[word][2]): 
                words_count += 1
        return words_count
        
    def NLP_preprocess(self, X ,y):
        dataset_joined = X.join(y)
        comment_phrases = list(dataset_joined.comment.value_counts().index[: 10])
        
        dataset_joined['is_comment'] = (~np.isin(dataset_joined.comment, comment_phrases)).astype(int)
        
        aggressive_comments = dataset_joined[(dataset_joined['is_comment'] == True) & (dataset_joined.is_aggressive == True)].comment.values
        normal_comments = dataset_joined[(dataset_joined['is_comment'] == True) & (dataset_joined.is_aggressive == False)].comment.values
        
        stop_words = ['на', 'по', 'с', 'в', 'что', 'и', 'а']

        for sentence in normal_comments:
            for word in sentence.split(" "):
                word = word.lower().replace(',', '').replace('.', '')
                if (word in stop_words): continue
                if (word in self.counter_words.keys()):
                    self.counter_words[word][0] += 1
                else: self.counter_words[word] = [1, 0]

        for sentence in aggressive_comments:
            for word in sentence.split(" "):
                word = word.lower().replace(',', '').replace('.', '')
                if (word in stop_words): continue
                if (word in self.counter_words.keys()):
                    self.counter_words[word][1] += 1
                else: self.counter_words[word] = [0, 1]
        
        
        count_all_words = np.array(list(map(lambda x: np.array(x), np.array(list(self.counter_words.items())).T[1]))).T
        
        count_normal_words = count_all_words[0].sum()
        count_aggressive_words = count_all_words[1].sum()

        for word_pair in list(self.counter_words.items()):
            if (word_pair[1][1] == 0 and word_pair[1][0] > 0):
                self.counter_words[word_pair[0]].append("normal")
                continue

            if (word_pair[1][0] == 0 and word_pair[1][1] > 0):
                self.counter_words[word_pair[0]].append("aggressive")
                continue

            ratio_aggressive = word_pair[1][1] / count_aggressive_words
            ratio_normal = word_pair[1][0] / count_normal_words

            if (ratio_aggressive / ratio_normal >= 3):
                self.counter_words[word_pair[0]].append("aggressive")
                continue

            if (ratio_normal / ratio_aggressive >= 3):
                self.counter_words[word_pair[0]].append("normal")
                continue

            self.counter_words[word_pair[0]].append("neutral")

    def add_features(self, X):
        comment_phrases = list(X.comment.value_counts().index[: 5]) + ["---"]
        
        X["is_comment"] = (~np.isin(X.comment, comment_phrases)).astype(int)
        X['dttm'] = pd.to_datetime(X.dttm)
        X['hour'] = X.dttm.apply(lambda x: x.hour)
        X['traff_jam'] = ((X.hour > 6) & (X.hour < 10)) | ((X.hour > 17) & (X.hour < 23))
        X['traff_jam'] = X.traff_jam.astype(int)
        X['weekday'] = X.dttm.apply(lambda x: x.weekday())
        X['holiday'] = (X.weekday >= 5).astype(int)
        X["count_words"] = [-1] * X.shape[0]
        X.loc[X.is_comment == True, "count_words"] = X[X.is_comment == True].comment.apply(lambda x: self.count_words(x))
        X["speed"] = X.distance / (X.duration / 60)
        X['agg_words'] = X.comment.apply(lambda x: self.check_sentence(x, "aggressive"))
        X['normal_words'] = X.comment.apply(lambda x: self.check_sentence(x, "normal"))
        X['distance_thresh'] = ((X.distance > 5) & (X.distance < 20)).astype(int)
        
        return X
    
    def gen_speed(self, tracks):
        tracks['speed'] = np.zeros(tracks.shape[0])
        for i in tqdm(range(1, len(tracks))):
            tracks.iloc[i, tracks.columns.get_loc('speed')] = get_speed(tracks.iloc[i-1, tracks.columns.get_loc('lat_')], tracks.iloc[i-1, tracks.columns.get_loc('lon_')],
                                        tracks.iloc[i, tracks.columns.get_loc('lat_')], tracks.iloc[i, tracks.columns.get_loc('lon_')], tracks.iloc[i-1, tracks.columns.get_loc('dt')], tracks.iloc[i, tracks.columns.get_loc('dt')])
        return tracks
    
    def estimate(self, X, y):
        return roc_auc_score(y, self.predict_proba(X, add_feat=False))
    
    def train_test_split_(self, X, y, test_size, X_ss=None, y_ss=None, random_state=RANDOM_STATE):
        if (X_ss is not None):
            X_ss_full, y_ss_full = self.label_shuffle(X, y, X_ss, y_ss, random_state = random_state)
            
            len_train = len(X_ss_full) - round(len(X_ss_full) * test_size)
            
            x_train = X_ss_full[: len_train]
            x_train.drop('ss', axis = 1, inplace = True)
            
            x_test = X_ss_full.iloc[len_train + 1:]
            x_test = x_test[x_test.ss == 0]
            x_test.drop('ss', axis = 1, inplace = True)
            
            y_train = y_ss_full[: len_train]
            y_train.drop('ss', axis = 1, inplace = True)
            
            y_test = y_ss_full.iloc[len_train + 1:]
            y_test = y_test[y_test.ss == 0]
            y_test.drop('ss', axis = 1, inplace = True)
            
            return (x_train, x_test, y_train, y_test)
        
        len_train = len(X) - round(len(X) * test_size)
        
        X = X.sample(frac=1, random_state=random_state)
        y = y.sample(frac=1, random_state=random_state)
        
        return (X[: len_train], X[len_train :], y[: len_train], y[len_train :])
    
    def train(self, X_train, X_test, y_train, y_test, categorical_feature, random_state=RANDOM_STATE):
        print(f"Train size: {X_train.shape}")
        print(f"Test size: {X_test.shape}")
        self.model = CatBoostClassifier(iterations=2000,
                           depth=2,
                           silent=True,
                           loss_function='Logloss',
                           class_weights=(1, 2),
                           random_state=random_state)

        self.model.fit(X_train, y_train, cat_features=categorical_features)
        
        return self.estimate(X_test, y_test)
    
    def label_shuffle(self, X, y, X_ss, y_ss, random_state=RANDOM_STATE):
        X_ss['ss'] = 1
        y_ss = y_ss.to_frame()
        y_ss['ss'] = 1

        X['ss'] = 0
        y['ss'] = 0

        X_ss_full = pd.concat([X, X_ss]).sample(frac=1, random_state=random_state)
        y_ss_full = pd.concat([y, y_ss]).sample(frac=1, random_state=random_state)
        
        return (X_ss_full, y_ss_full)
    
    def train_cross_validation(self, X, y, k, categorical_features, X_ss=None, y_ss=None, random_state=RANDOM_STATE):
        chunk_size = len(X) / k
        chunks_size = [(i*chunk_size, i*chunk_size + chunk_size) for i in range(k)]
        
        result_score = []
        
        print(f"Part size: {chunk_size}")
        
        if (X_ss is not None):
            X_ss_full, y_ss_full = self.label_shuffle(X, y, X_ss, y_ss, random_state = random_state)
            
            for chunkIndex in range(len(chunks_size)):
                x_test = X_ss_full[int(chunks_size[chunkIndex][0]) : int(chunks_size[chunkIndex][1])]
                y_test = y_ss_full[int(chunks_size[chunkIndex][0]) : int(chunks_size[chunkIndex][1])]
                
                x_train = X_ss_full.drop(x_test.index, axis = 0)
                y_train = y_ss_full.drop(y_test.index, axis = 0)
                
                x_test = x_test[x_test.ss == 0]
                y_test = y_test[y_test.ss == 0]
                
                x_train.drop('ss', axis = 1, inplace = True)
                y_train.drop('ss', axis = 1, inplace = True)
                x_test.drop('ss', axis = 1, inplace = True)
                y_test.drop('ss', axis = 1, inplace = True)
                
                score = self.train(x_train, x_test, y_train, y_test, categorical_features)
                
                print(f"Chunk {chunkIndex}; Score: {score}")
                
                result_score.append((chunks_size[chunkIndex], score))
        else:            
            for chunkIndex in range(len(chunks_size)):
                x_test = X[int(chunks_size[chunkIndex][0]) : int(chunks_size[chunkIndex][1])]
                y_test = y[int(chunks_size[chunkIndex][0]) : int(chunks_size[chunkIndex][1])]
                
                x_train = X.drop(x_test.index, axis = 0)
                y_train = y.drop(y_test.index, axis = 0)
                
                score = self.train(x_train, x_test, y_train, y_test, categorical_features)
                
                print(f"Chunk {chunkIndex}; Score: {score}")
                
                result_score.append((chunks_size[chunkIndex], score))
            
        print(f"Mean score: {sum(list(map(lambda x: x[1], result_score))) / k}")
        
        return result_score
    
    def fit_ss(self, X, y, numeric_features, categorial_features, X_ss, y_ss, cross_validation=False, random_state=RANDOM_STATE):
        self.counter_words = {}
        
        X_ = X
        y_ = y
        
        self.NLP_preprocess(pd.concat([X_, X_ss]), pd.concat([y_, y_ss]))
        X_ = self.add_features(X_)[numeric_features + categorical_features]
        
        X_ss = self.add_features(X_ss)[numeric_features + categorical_features]
        
        if (not cross_validation):
            X_train, X_test, y_train, y_test = self.train_test_split_(X_, y_, test_size=0.2, X_ss=X_ss, y_ss=y_ss, random_state=random_state)
            return self.train(X_train, X_test, y_train, y_test, categorical_features)
        else:
            return self.train_cross_validation(X_, y_, 5, categorical_features, X_ss=X_ss, y_ss=y_ss, random_state=random_state)
        
        
    def fit(self, X, y, numeric_features, categorial_features, cross_validation=False, random_state=RANDOM_STATE):
        self.counter_words = {}
        
        X_ = X
        y_ = y
        
        self.NLP_preprocess(X_, y_)
        X_ = self.add_features(X_)[numeric_features + categorical_features]

        if (not cross_validation):
            X_train, X_test, y_train, y_test = self.train_test_split_(X_, y_, test_size=0.2, random_state=random_state)
            return self.train(X_train, X_test, y_train, y_test, categorical_features)
        else:
            return self.train_cross_validation(X_, y_, 5, categorical_features, random_state=random_state)

    def fit_tracks(self, tracks, random_state=RANDOM_STATE):
        print("Preprocessing tracks data...")
        X, y = self.tracks_preprocess(tracks, self.TRACKS_CHUNK_SIZE, self.TRACKS_MULTIPLIER)

        print("Training MiniRocket...")
        self.model_rocket = MiniRocket()
        self.model_rocket.fit(X, y)

        print("MiniRocket transforming...")
        X_train_transform = self.model_rocket.transform(X, y)

        print("Training logistic regression...")
        # self.model_tracks = RidgeClassifierCV(normalize = True)
        self.model_tracks = RidgeClassifier(normalize = True, random_state=RANDOM_STATE)
        self.model_tracks.fit(X_train_transform, y)

        print(y.sum(), self.model_tracks.predict(X_train_transform).sum())

        # print(f"Score: {self.model_tracks.score(X_train_transform, y)}")
        print(f"Roc AUC score: {roc_auc_score(y, self.model_tracks.predict(X_train_transform))}")
    
    def predict_tracks(self, tracks, gen_speed=True, drop_duplicates=True):
        print("Preprocessing tracks data...")
        X = tracks.copy()
        if gen_speed:
            print(" Generating speed...")
            X = self.gen_speed(X)
        print(" Converting time series...")
        X = self.tracks_preprocess(X, self.TRACKS_CHUNK_SIZE, self.TRACKS_MULTIPLIER, labled=False, drop_duplicates=drop_duplicates)
        
        print("MiniRocket transforming...")
        X_transform = self.model_rocket.transform(X)

        return self.model_tracks.predict(X_transform)

    def predict_proba(self, X, add_feat=True):
        if (add_feat): X = self.add_features(X)
        
        X = X[numeric_features + categorical_features]
        
        return self.model.predict_proba(X).T[1]
    
    def predict(self, X, add_feat=True):
        if (add_feat): X = self.add_features(X)
        
        X = X[numeric_features + categorical_features]
        
        return self.model.predict(X)
    
    def predict_thresh(self, X, thresh_above, thresh_below):
        y_unlab_full = self.predict_proba(X)
        
        y_unlab = pd.Series([-1 for i in range(len(X))])
        
        print("Thresh above: {}".format(sum(y_unlab_full >= thresh_above) / len(y_unlab_full)))
        print("Thresh below: {}".format(sum(y_unlab_full <= thresh_below) / len(y_unlab_full)))
        
        y_unlab.iloc[np.where(y_unlab_full >= thresh_above)] = 1
        y_unlab.iloc[np.where(y_unlab_full <= thresh_below)] = 0
        
        return y_unlab

    # undersampling method deletes some extra non aggressive values
    def undersampling(self, X, multiplier):
        aggressive_count = sum(X.is_aggressive==1)
        non_aggressive_ind = X[X.is_aggressive==0].index

        # number of aggressive and non-aggressive labels is the same
        random_indices = np.random.choice(non_aggressive_ind, int(aggressive_count*multiplier), replace=False)
        return pd.concat([X.loc[random_indices], X[X.is_aggressive==1]])

    def split(self, arr, chunk_size = 15):
        result = []
        #get right length of arr so that it equally splits into chunks
        length = len(arr)
        split_length = length - (length%chunk_size)
                
        for i in range(split_length)[chunk_size::chunk_size]:
            result.append(arr[i-chunk_size:i])

        return np.array(result)

    # make df, so that each row has whole order speeds time series
    def make_nested(self, tracks, chunk_size, multiplier, labled, drop_duplicates):
        orders = tracks.copy()
        if drop_duplicates:
            orders = tracks.drop_duplicates('order_id', keep='last')
        if labled:
            orders = self.undersampling(orders, multiplier)
        y_labels = []
        X_train = []
        for order in tqdm(orders['order_id']):
            order_df = tracks[tracks.order_id == order]
            order_df.loc[0, 'speed'] = 0

            if order_df.shape[0] < chunk_size:
                continue

            splitted_arrs = self.split(order_df.values, chunk_size)
            for arr in splitted_arrs:
                if labled:
                    is_aggressive = arr[0][6]
                    y_labels.append(is_aggressive)
                speed_series = []
                for row in arr:  
                    # append only speed and dt values 
                    speed_series.append(row[5])
                X_train.append(pd.Series(speed_series))
        return X_train, y_labels
    
    def tracks_preprocess(self, tracks, chunk_size, multiplier, labled=True, drop_duplicates=True):
        X_train, train_labels = self.make_nested(tracks, chunk_size, multiplier, labled, drop_duplicates)

        X_train = pd.DataFrame({'speed': X_train})
        if not labled: return X_train
        y_train = np.array(train_labels)

        return X_train, y_train