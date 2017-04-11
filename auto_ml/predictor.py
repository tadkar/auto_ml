import datetime
import math
import os
import random
import sys
import warnings

import dill
import pathos

import pandas as pd

# Ultimately, we (the authors of auto_ml) are responsible for building a project that's robust against warnings.
# The classes of warnings below are ones we've deemed acceptable. The user should be able to sit at a high level of abstraction, and not be bothered with the internals of how we're handing these things.
# Ignore all warnings that are UserWarnings or DeprecationWarnings. We'll fix these ourselves as necessary.
# warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
pd.options.mode.chained_assignment = None  # default='warn'

import scipy
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction import DictVectorizer
from sklearn.model_selection import GridSearchCV
from sklearn.metrics import mean_squared_error, brier_score_loss, make_scorer, accuracy_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer


from auto_ml import DataFrameVectorizer
from auto_ml import utils
from auto_ml import utils_categorical_ensembling
from auto_ml import utils_data_cleaning
from auto_ml import utils_feature_selection
from auto_ml import utils_model_training
from auto_ml import utils_models
from auto_ml import utils_scaling
from auto_ml import utils_scoring

xgb_installed = False
try:
    import xgboost as xgb
    xgb_installed = True
except ImportError:
    pass

keras_installed = False
try:
    from keras.models import Model
    keras_installed = True
except ImportError as e:
    keras_import_error = e
    pass

class Predictor(object):


    def __init__(self, type_of_estimator, column_descriptions, verbose=True, name=None):
        if type_of_estimator.lower() in ['regressor','regression', 'regressions', 'regressors', 'number', 'numeric', 'continuous']:
            self.type_of_estimator = 'regressor'
        elif type_of_estimator.lower() in ['classifier', 'classification', 'categorizer', 'categorization', 'categories', 'labels', 'labeled', 'label']:
            self.type_of_estimator = 'classifier'
        else:
            print('Invalid value for "type_of_estimator". Please pass in either "regressor" or "classifier". You passed in: ' + type_of_estimator)
            raise ValueError('Invalid value for "type_of_estimator". Please pass in either "regressor" or "classifier". You passed in: ' + type_of_estimator)
        self.column_descriptions = column_descriptions
        self.verbose = verbose
        self.trained_pipeline = None
        self._scorer = None
        self.date_cols = []
        # Later on, if this is a regression problem, we will possibly take the natural log of our y values for training, but we will still want to return the predictions in their normal scale (not the natural log values)
        self.took_log_of_y = False
        self.take_log_of_y = False

        self._validate_input_col_descriptions()

        self.grid_search_pipelines = []

        self.name = name


    def _validate_input_col_descriptions(self):
        found_output_column = False
        self.cols_to_ignore = []
        expected_vals = set(['categorical', 'text', 'nlp'])

        for key, value in self.column_descriptions.items():
            value = value.lower()
            self.column_descriptions[key] = value
            if value == 'output':
                self.output_column = key
                found_output_column = True
            elif value == 'date':
                self.date_cols.append(key)
            elif value == 'ignore':
                self.cols_to_ignore.append(key)
            elif value in expected_vals:
                pass
            else:
                raise ValueError('We are not sure how to process this column of data: ' + str(value) + '. Please pass in "output", "categorical", "ignore", "nlp", or "date".')
        if found_output_column is False:
            print('Here is the column_descriptions that was passed in:')
            print(self.column_descriptions)
            raise ValueError('In your column_descriptions, please make sure exactly one column has the value "output", which is the value we will be training models to predict.')

        # We will be adding one new categorical variable for each date col
        # Be sure to add it here so the rest of the pipeline knows to handle it as a categorical column
        for date_col in self.date_cols:
            self.column_descriptions[date_col + '_day_part'] = 'categorical'


    # We use _construct_pipeline at both the start and end of our training.
    # At the start, it constructs the pipeline from scratch
    # At the end, it takes FeatureSelection out after we've used it to restrict DictVectorizer, and adds final_model back in if we did grid search on it
    def _construct_pipeline(self, model_name='LogisticRegression', trained_pipeline=None, final_model=None, feature_learning=False, final_model_step_name='final_model'):

        pipeline_list = []


        if self.user_input_func is not None:
            if trained_pipeline is not None:
                pipeline_list.append(('user_func', trained_pipeline.named_steps['user_func']))
            elif self.transformation_pipeline is None:
                print('Including the user_input_func in the pipeline! Please remember to return X, and not modify the length or order of X at all.')
                print('Your function will be called as the first step of the pipeline at both training and prediction times.')
                pipeline_list.append(('user_func', FunctionTransformer(func=self.user_input_func, pass_y=False, validate=False)))

        # These parts will be included no matter what.
        if trained_pipeline is not None:
            pipeline_list.append(('basic_transform', trained_pipeline.named_steps['basic_transform']))
        else:
            pipeline_list.append(('basic_transform', utils_data_cleaning.BasicDataCleaning(column_descriptions=self.column_descriptions)))

        if self.perform_feature_scaling is True:
            if trained_pipeline is not None:
                pipeline_list.append(('scaler', trained_pipeline.named_steps['scaler']))
            else:
                pipeline_list.append(('scaler', utils_scaling.CustomSparseScaler(self.column_descriptions)))


        if trained_pipeline is not None:
            pipeline_list.append(('dv', trained_pipeline.named_steps['dv']))
        else:
            pipeline_list.append(('dv', DataFrameVectorizer.DataFrameVectorizer(sparse=True, sort=True, column_descriptions=self.column_descriptions)))


        if self.perform_feature_selection == True:
            if trained_pipeline is not None:
                # This is the step we are trying to remove from the trained_pipeline, since it has already been combined with dv using dv.restrict
                pass
            else:
                pipeline_list.append(('feature_selection', utils_feature_selection.FeatureSelectionTransformer(type_of_estimator=self.type_of_estimator, column_descriptions=self.column_descriptions, feature_selection_model='SelectFromModel') ))

        if trained_pipeline is not None:
            # TODO:
            # First, check and see if we have any steps with some version of keyword matching on something like 'intermediate_model_predictions' or 'feature_learning_model' or 'ensemble_model' or something like that in them.
            # add all of those steps
            # then try to add in the final_model that was passed in as a param
            # if it's none, then we've already added in the final model with our keyword matching above!
            for step in trained_pipeline.steps:
                step_name = step[0]
                if step_name[-6:] == '_model':
                    pipeline_list.append((step_name, trained_pipeline.named_steps[step_name]))

            # Handling the case where we have run gscv on just the final model itself, and we now need to integrate it back into the rest of the pipeline
            if final_model is not None:
                pipeline_list.append((final_model_step_name, final_model))
            # else:
            #     pipeline_list.append(('final_model', trained_pipeline.named_steps['final_model']))
        else:
            final_model = utils_models.get_model_from_name(model_name, training_params=self.training_params)
            pipeline_list.append(('final_model', utils_model_training.FinalModelATC(model=final_model, type_of_estimator=self.type_of_estimator, ml_for_analytics=self.ml_for_analytics, name=self.name, scoring_method=self._scorer, feature_learning=feature_learning)))

        constructed_pipeline = Pipeline(pipeline_list)
        return constructed_pipeline


    def _get_estimator_names(self):
        if self.type_of_estimator == 'regressor':
            if xgb_installed:
                base_estimators = ['XGBRegressor']
            else:
                base_estimators = ['GradientBoostingRegressor']

            if self.compare_all_models != True:
                return base_estimators
            else:
                base_estimators.append('RANSACRegressor')
                base_estimators.append('RandomForestRegressor')
                base_estimators.append('LinearRegression')
                base_estimators.append('AdaBoostRegressor')
                base_estimators.append('ExtraTreesRegressor')
                return base_estimators

        elif self.type_of_estimator == 'classifier':
            if xgb_installed:
                base_estimators = ['XGBClassifier']
            else:
                base_estimators = ['GradientBoostingClassifier']
            if self.compare_all_models != True:
                return base_estimators
            else:
                base_estimators.append('LogisticRegression')
                base_estimators.append('RandomForestClassifier')
                return base_estimators

        else:
            raise('TypeError: type_of_estimator must be either "classifier" or "regressor".')

    def _prepare_for_training(self, X):

        # We accept input as either a DataFrame, or as a list of dictionaries. Internally, we use DataFrames. So if the user gave us a list, convert it to a DataFrame here.
        if isinstance(X, list):
            X_df = pd.DataFrame(X)
            del X
        else:
            X_df = X

        # To keep this as light in memory as possible, immediately remove any columns that the user has already told us should be ignored
        if len(self.cols_to_ignore) > 0:
            X_df = utils.safely_drop_columns(X_df, self.cols_to_ignore)

        # Having duplicate columns can really screw things up later. Remove them here, with user logging to tell them what we're doing
        X_df = utils.drop_duplicate_columns(X_df)

        # If we're writing training results to file, create the new empty file name here
        if self.write_gs_param_results_to_file:
            self.gs_param_file_name = 'most_recent_pipeline_grid_search_result.csv'
            try:
                os.remove(self.gs_param_file_name)
            except:
                pass

        # Remove the output column from the dataset, and store it into the y varaible
        y = list(X_df.pop(self.output_column))

        # Drop all rows that have an empty value for our output column
        # User logging so they can adjust if they pass in a bunch of bad values:
        X_df, y = utils.drop_missing_y_vals(X_df, y, self.output_column)

        # If this is a classifier, try to turn all the y values into proper ints
        # Some classifiers play more nicely if you give them category labels as ints rather than strings, so we'll make our jobs easier here if we can.
        if self.type_of_estimator == 'classifier':
            # The entire column must be turned into floats. If any value fails, don't convert anything in the column to floats
            try:
                y_ints = []
                for val in y:
                    y_ints.append(int(val))
                y = y_ints
            except:
                pass
        else:
            # If this is a regressor, turn all the values into floats if possible, and remove this row if they cannot be turned into floats
            indices_to_delete = []
            y_floats = []
            bad_vals = []
            for idx, val in enumerate(y):
                try:
                    float_val = utils_data_cleaning.clean_val(val)
                    y_floats.append(float_val)
                except ValueError as err:
                    indices_to_delete.append(idx)
                    bad_vals.append(val)

            y = y_floats

            # Even more verbose logging here since these values are not just missing, they're strings for a regression problem
            if len(indices_to_delete) > 0:
                print('The y values given included some bad values that the machine learning algorithms will not be able to train on.')
                print('The rows at these indices have been deleted because their y value could not be turned into a float:')
                print(indices_to_delete)
                print('These were the bad values')
                print(bad_vals)
                X_df = X_df.drop(X_df.index(indices_to_delete))

        return X_df, y


    def _consolidate_pipeline(self, transformation_pipeline, final_model=None):
        # First, restrict our DictVectorizer or DataFrameVectorizer
        # This goes through and has DV only output the items that have passed our support mask
        # This has a number of benefits: speeds up computation, reduces memory usage, and combines several transforms into a single, easy step
        # It also significantly reduces the size of dv.vocabulary_ which can get quite large

        dv = transformation_pipeline.named_steps['dv']

        try:
            feature_selection = transformation_pipeline.named_steps['feature_selection']
            feature_selection_mask = feature_selection.support_mask
            dv.restrict(feature_selection_mask)
        except KeyError:
            pass

        # We have overloaded our _construct_pipeline method to work both to create a new pipeline from scratch at the start of training, and to go through a trained pipeline in exactly the same order and steps to take a dedicated FeatureSelection model out of an already trained pipeline
        # In this way, we ensure that we only have to maintain a single centralized piece of logic for the correct order a pipeline should follow
        trained_pipeline_without_feature_selection = self._construct_pipeline(trained_pipeline=transformation_pipeline, final_model=final_model)

        return trained_pipeline_without_feature_selection

    def set_params_and_defaults(self, user_input_func=None, optimize_final_model=None, write_gs_param_results_to_file=True, perform_feature_selection=None, verbose=True, X_test=None, y_test=None, ml_for_analytics=True, take_log_of_y=None, model_names=None, perform_feature_scaling=True, calibrate_final_model=False, _scorer=None, scoring=None, verify_features=False, training_params=None, grid_search_params=None, compare_all_models=False, cv=2, feature_learning=False, fl_data=None):

        self.user_input_func = user_input_func
        self.optimize_final_model = optimize_final_model
        self.write_gs_param_results_to_file = write_gs_param_results_to_file
        self.ml_for_analytics = ml_for_analytics
        self.X_test = X_test
        self.y_test = y_test
        if self.type_of_estimator == 'regressor':
            self.take_log_of_y = take_log_of_y

        # We expect model_names to be a list of strings
        if isinstance(model_names, str):
            # If the user passes in a single string, put it in a list
            self.model_names = [model_names]
        else:
            self.model_names = model_names

        self.perform_feature_scaling = perform_feature_scaling
        self.calibrate_final_model = calibrate_final_model
        self.scoring = scoring
        self.training_params = training_params
        self.user_gs_params = grid_search_params
        if self.user_gs_params is not None:
            self.optimize_final_model = True
        self.compare_all_models = compare_all_models
        self.cv = cv

        self.perform_feature_selection = perform_feature_selection
        self.feature_learning = feature_learning
        if self.feature_learning == True:
            if fl_data is None:
                print('Saw that feature_learning is True, but there is no data passed in for fl_data, which is needed to train the feature_learning estimator')
                warnings.warn('Please pass in fl_data which is the dataset that will be used to train the feature_learning estimator.')
                raise ValueError('The data passed in for fl_data is missing')
            self.fl_data = fl_data

            if self.perform_feature_scaling == True:
                print('Heard that we should not perform feature_scaling, but we should include feature_learning. Note that feature_scaling is typically useful for deep learning, which is what we use for feature_learning. If you want a little more model accuracy from the feature_learning step, consider not passing in perform_feature_scaling=True')
                warnings.warn('Consider allowing auto_ml to perform_feature_scaling in conjunction with feature_learning')

            if self.perform_feature_selection == True:
                print('We are not currently supporting perform_feature_selection with this release of feature_learning. We will override perform_feature_selection to False and continue with training.')
                warnigns.warn('perform_feature_selection=True is not currently supported with feature_learning.')
            self.perform_feature_selection = False

            if keras_installed != True:
                print('feature_learning requires Keras to be installed.')
                print('When we tried to import Model from Keras, we ran into the following error:')
                print(keras_import_error)
                print('Raising that error now, since feature_learning will not run without Keras')
                raise(keras_import_error)

        self.transformation_pipeline = None



    # We are taking in scoring here to deal with the unknown behavior around multilabel classification below
    def _clean_data_and_prepare_for_training(self, data, scoring):

        X_df, y = self._prepare_for_training(data)

        if self.take_log_of_y:
            y = [math.log(val) for val in y]
            self.took_log_of_y = True

        self.X_df = X_df
        self.y = y

        # Unless the user has told us to, don't perform feature selection unless we have a pretty decent amount of data
        if self.perform_feature_selection is None:
            if len(X_df.columns) < 50 or len(X_df) < 100000:
                self.perform_feature_selection = False
            else:
                self.perform_feature_selection = True

        if self.model_names is not None:
            estimator_names = self.model_names
        else:
            estimator_names = self._get_estimator_names()


        # TODO: we're not using ClassificationScorer for multilabel classification. Why?
        if self.type_of_estimator == 'classifier':
            if len(set(y)) > 2 and self.scoring is None:
                self.scoring = 'accuracy_score'
            else:
                scoring = utils_scoring.ClassificationScorer(self.scoring)
            self._scorer = scoring
        else:
            scoring = utils_scoring.RegressionScorer(self.scoring)
            self._scorer = scoring


        return X_df, y, estimator_names


    def train(self, raw_training_data, user_input_func=None, optimize_final_model=None, write_gs_param_results_to_file=True, perform_feature_selection=None, verbose=True, X_test=None, y_test=None, ml_for_analytics=True, take_log_of_y=None, model_names=None, perform_feature_scaling=True, calibrate_final_model=False, _scorer=None, scoring=None, verify_features=False, training_params=None, grid_search_params=None, compare_all_models=False, cv=2, feature_learning=False, fl_data=None):

        self.set_params_and_defaults(user_input_func=user_input_func, optimize_final_model=optimize_final_model, write_gs_param_results_to_file=write_gs_param_results_to_file, perform_feature_selection=perform_feature_selection, verbose=verbose, X_test=X_test, y_test=y_test, ml_for_analytics=ml_for_analytics, take_log_of_y=take_log_of_y, model_names=model_names, perform_feature_scaling=perform_feature_scaling, calibrate_final_model=calibrate_final_model, _scorer=_scorer, scoring=scoring, verify_features=verify_features, training_params=training_params, grid_search_params=grid_search_params, compare_all_models=compare_all_models, cv=cv, feature_learning=feature_learning, fl_data=fl_data)

        if verbose:
            print('Welcome to auto_ml! We\'re about to go through and make sense of your data using machine learning, and give you a production-ready pipeline to get predictions with.\n')
            print('If you have any issues, or new feature ideas, let us know at https://github.com/ClimbsRocks/auto_ml')


        X_df, y, estimator_names = self._clean_data_and_prepare_for_training(raw_training_data, scoring)

        if self.feature_learning == True:
            fl_data_cleaned, fl_y, _ = self._clean_data_and_prepare_for_training(fl_data, scoring)

            combined_training_data = pd.concat([X_df, fl_data_cleaned], axis=0)
            combined_y = y + fl_y
            combined_transformed_data = self.fit_transformation_pipeline(combined_training_data, combined_y, estimator_names[0])

            # TODO: if feature_learning, then fit the transformation pipeline on the combined dataset
            # Then, for MVP, transform the fl_data in a different step
            # THEN, after we've fit our feature_learning step, transform the X_df through the entire transformation pipeline which includes the feature_learning features
            # else, do our normal workflow
            # We both fit the transformation pipeline, and transform X_df in this step

            # For performance reasons, I believe it is critical to only have one transformation pipeline, no matter how many estimators we eventually build on top. Getting predictions from a trained estimator is typically super quick. We can easily get predictions from 10 trained models in a production-ready amount of time.But the transformation pipeline is not so quick that we can duplicate it 10 times.
            # Simplified approach: we do not fit the transformation_pipeline on the fl_data. This reduces computation time somewhat, and everything that's in fl_data should be a subset of what's in our X_df data
            # We can change this in v2, but this is a conscious design decision for now
            # FUTURE IMPROVEMENT: just grab our already_transformed fl_data and X_df
            fl_data = self.transformation_pipeline.transform(fl_data)

            # TODO:
            if self.type_of_estimator == 'classifier':
                fl_estimator_names = ['DeepLearningClassifier']
            elif self.type_of_estimator == 'regressor':
                fl_estimator_names = ['DeepLearningRegressor']

            # fit a train_final_estimator
            feature_learning_step = self.train_ml_estimator(fl_estimator_names, self._scorer, fl_data, fl_y, feature_learning=feature_learning)

            # Split off the final layer/find a way to get the output from the penultimate layer
            fl_model = feature_learning_step.model

            feature_output_model = Model(inputs=fl_model.model.input, outputs=fl_model.model.get_layer('penultimate_layer').output)
            feature_learning_step.model = feature_output_model

            # Make sure I know how many neurons are in that final layer- settled on 10 always

            # Add those to the list in our DV so we know what to do with them for analytics purposes
            feature_learning_names = []
            for idx in range(10):
                feature_learning_names.append('feature_learning_' + str(idx + 1))

            self.transformation_pipeline.named_steps['dv'].feature_names_ += feature_learning_names
            # modify FinalModelATC so it has a transform function that passes through all of X and just hstacks a few new columns on the end

            # add the estimator to the end of our transformation pipeline
            self.transformation_pipeline = self._construct_pipeline(trained_pipeline=self.transformation_pipeline, final_model=feature_learning_step, final_model_step_name='feature_learning_model')

            # FUTURE IMPROVEMENT: don't pass this back through the entire transformation_pipeline. instead, grab the X_df we've already transformed above, and pass it through only the feature_learning_step.transform() method, since that's the only computation we haven't done on it yet, and what's below is a lot of duplicate computation
            X_df = self.transformation_pipeline.transform(X_df)


        else:
            X_df = self.fit_transformation_pipeline(X_df, y, estimator_names[0])

        # This is our main logic for how we train the final model
        self.trained_final_model = self.train_ml_estimator(estimator_names, self._scorer, X_df, y)

        # Calibrate the probability predictions from our final model
        if self.calibrate_final_model is True:
            self.trained_final_model.model = self._calibrate_final_model(self.trained_final_model.model, X_test, y_test)

        self.trained_pipeline = self._consolidate_pipeline(self.transformation_pipeline, self.trained_final_model)

        # verify_features is not enabled by default. It adds a significant amount to the file size of the saved pipelines.
        # If you are interested in submitting a PR to reduce the saved file size, there are definitely some optimizations you can make!
        if verify_features == True:
            self._prepare_for_verify_features()

        # Delete values that we no longer need that are just taking up space.
        del self.X_test
        del self.y_test
        del self.grid_search_pipelines
        del X_df


    def _prepare_for_verify_features(self):
        # Save the features we used for training to our FinalModelATC instance.
        # This lets us provide useful information to the user when they call .predict(data, verbose=True)
        trained_feature_names = self._get_trained_feature_names()
        self.trained_pipeline.set_params(final_model__training_features=trained_feature_names)
        # We will need to know which columns are categorical/ignored/nlp when verifying features
        self.trained_pipeline.set_params(final_model__column_descriptions=self.column_descriptions)


    def _calibrate_final_model(self, trained_model, X_test, y_test):

        if X_test is None or y_test is None:
            print('X_test or y_test was not present while trying to calibrate the final model')
            print('Please pass in both X_test and y_test to calibrate the final model')
            print('Skipping model calibration')
            return trained_model

        print('Now calibrating the final model so the probability predictions line up with the observed probabilities in the X_test and y_test datasets you passed in.')
        print('Note: the validation scores printed above are truly validation scores: they were scored before the model was calibrated to this data.')
        print('However, now that we are calibrating on the X_test and y_test data you gave us, it is no longer accurate to call this data validation data, since the model is being calibrated to it. As such, you must now report a validation score on a different dataset, or report the validation score used above before the model was calibrated to X_test and y_test. ')

        if len(X_test) < 1000:
            calibration_method = 'sigmoid'
        else:
            calibration_method = 'isotonic'

        calibrated_classifier = CalibratedClassifierCV(trained_model, method=calibration_method, cv='prefit')

        # We need to make sure X_test has been processed the exact same way y_test has.
        X_test_processed = self.transformation_pipeline.transform(X_test)

        try:
            calibrated_classifier = calibrated_classifier.fit(X_test_processed, y_test)
        except TypeError as e:
            if scipy.sparse.issparse(X_test_processed):
                X_test_processed = X_test_processed.toarray()

                calibrated_classifier = calibrated_classifier.fit(X_test_processed, y_test)
            else:
                raise(e)

        return calibrated_classifier


    def fit_single_pipeline(self, X_df, y, model_name, feature_learning=False):

        full_pipeline = self._construct_pipeline(model_name=model_name, feature_learning=feature_learning)
        ppl = full_pipeline.named_steps['final_model']
        if self.verbose:
            print('\n\n********************************************************************************************')
            if self.name is not None:
                print(self.name)
            print('About to fit the pipeline for the model ' + model_name + ' to predict ' + self.output_column)
            print('Started at:')
            start_time = datetime.datetime.now().replace(microsecond=0)
            print(start_time)

        ppl.fit(X_df, y)

        if self.verbose:
            print('Finished training the pipeline!')
            print('Total training time:')
            print(datetime.datetime.now().replace(microsecond=0) - start_time)

        self.trained_final_model = ppl
        self.print_results(model_name)

        return ppl


    # We have broken our model training into separate components. The first component is always going to be fitting a transformation pipeline. The great part about separating the feature transformation step is that now we can perform other work on the final step, and not have to repeat the sometimes time-consuming step of the transformation pipeline.
    # NOTE: if included, we will be fitting a feature selection step here. This can get messy later on with ensembling if we end up training on different y values.
    def fit_transformation_pipeline(self, X_df, y, model_name):
        ppl = self._construct_pipeline(model_name=model_name)
        ppl.steps.pop()

        # We are intentionally overwriting X_df here to try to save some memory space
        X_df = ppl.fit_transform(X_df, y)

        self.transformation_pipeline = ppl

        return X_df


    def print_results(self, model_name):
        if self.ml_for_analytics and model_name in ('LogisticRegression', 'RidgeClassifier', 'LinearRegression', 'Ridge'):
            self._print_ml_analytics_results_linear_model()

        elif self.ml_for_analytics and model_name in ['RandomForestClassifier', 'RandomForestRegressor', 'XGBClassifier', 'XGBRegressor', 'GradientBoostingRegressor', 'GradientBoostingClassifier', 'LGBMRegressor', 'LGBMClassifier']:
            self._print_ml_analytics_results_random_forest()


    def fit_grid_search(self, X_df, y, gs_params, feature_learning=False):

        model = gs_params['model']
        # Sometimes we're optimizing just one model, sometimes we're comparing a bunch of non-optimized models.
        if isinstance(model, list):
            model = model[0]

        if len(gs_params['model']) == 1:
            # Delete this so it doesn't show up in our logging
            del gs_params['model']
        model_name = utils_models.get_name_from_model(model)

        full_pipeline = self._construct_pipeline(model_name=model_name, feature_learning=feature_learning)
        ppl = full_pipeline.named_steps['final_model']

        if self.verbose:
            grid_search_verbose = 5
        else:
            grid_search_verbose = 0


        gs = GridSearchCV(
            # Fit on the pipeline.
            ppl,
            # Two splits of cross-validation, by default
            cv=self.cv,
            param_grid=gs_params,
            # Train across all cores.
            n_jobs=-1,
            # Be verbose (lots of printing).
            verbose=grid_search_verbose,
            # Print warnings when we fail to fit a given combination of parameters, but do not raise an error.
            # Set the score on this partition to some very negative number, so that we do not choose this estimator.
            error_score=-1000000000,
            scoring=self._scorer.score,
            # Don't allocate memory for all jobs upfront. Instead, only allocate enough memory to handle the current jobs plus an additional 50%
            pre_dispatch='1.5*n_jobs'
        )

        if self.verbose:
            print('\n\n********************************************************************************************')
            if self.optimize_final_model == True:
                print('About to run GridSearchCV on the pipeline for the model ' + model_name + ' to predict ' + self.output_column)
            else:
                print('About to run GridSearchCV on the pipeline for several models to predict ' + self.output_column)
                # Note that we will only report analytics results on the final model that ultimately gets selected, and trained on the entire dataset

        gs.fit(X_df, y)

        if self.write_gs_param_results_to_file:
            utils.write_gs_param_results_to_file(gs, self.gs_param_file_name)

        if self.verbose:
            self.print_training_summary(gs)

        self.trained_final_model = gs.best_estimator_
        if 'model' in gs.best_params_:
            model_name = gs.best_params_['model']
            self.print_results(model_name)

        return gs


    def create_gs_params(self, model_name):
        grid_search_params = {}

        raw_search_params = utils_models.get_search_params(model_name)

        for param_name, param_list in raw_search_params.items():
            # We need to tell GS where to set these params. In our case, it is on the "final_model" object, and specifically the "model" attribute on that object
            grid_search_params['model__' + param_name] = param_list

        # Overwrite with the user-provided gs_params if they're provided
        if self.user_gs_params is not None:
            print('Using the grid_search_params you passed in:')
            print(self.user_gs_params)
            grid_search_params.update(self.user_gs_params)
            print('Here is our final list of grid_search_params:')
            print(grid_search_params)
            print('Please note that if you want to set the grid search params for the final model specifically, they need to be prefixed with: "model__"')

        return grid_search_params

    # When we go to perform hyperparameter optimization, the hyperparameters for a GradientBoosting model will not at all align with the hyperparameters for an SVM. Doing all of that in one giant GSCV would throw errors. So we train each model in it's own grid search.
    def train_ml_estimator(self, estimator_names, scoring, X_df, y, feature_learning=False):

        # Use Case 1: Super straightforward: just train a single, non-optimized model
        if len(estimator_names) == 1 and self.optimize_final_model != True:
            trained_final_model = self.fit_single_pipeline(X_df, y, estimator_names[0], feature_learning=feature_learning)

        # Use Case 2: Compare a bunch of models, but don't optimize any of them
        elif len(estimator_names) > 1 and self.optimize_final_model != True:
            grid_search_params = {}

            final_model_models = map(utils_models.get_model_from_name, estimator_names)

            # We have to use GSCV here to choose between the different models
            grid_search_params['model'] = list(final_model_models)

            self.grid_search_params = grid_search_params

            gscv_results = self.fit_grid_search(X_df, y, grid_search_params)

            trained_final_model = gscv_results.best_estimator_

        # Use Case 3: One model, and optimize it!
        # Use Case 4: Many models, and optimize them!
        elif self.optimize_final_model == True:
            # Use Cases 3 & 4 are clearly highly related

            all_gs_results = []

            # If we just have one model, this will obviously be a very simple loop :)
            for model_name in estimator_names:

                grid_search_params = self.create_gs_params(model_name)
                # Adding model name to gs params just to help with logging
                grid_search_params['model'] = [utils_models.get_model_from_name(model_name)]
                # grid_search_params['model_name'] = model_name
                self.grid_search_params = grid_search_params

                gscv_results = self.fit_grid_search(X_df, y, grid_search_params, feature_learning=feature_learning)

                all_gs_results.append(gscv_results)

            # Grab the first one by default
            self.trained_final_model = all_gs_results[0].best_estimator_
            trained_final_model = all_gs_results[0].best_estimator_
            best_score = all_gs_results[0].best_score_

            # Iterate through the rest, and see if any are better!
            for result in all_gs_results[1:]:
                if result.best_score_ > best_score:
                    trained_final_model = result.best_estimator_
                    best_score = result.best_score_

            # If we wanted to do something tricky, here would be the place to do it
                # Train the final model up on more epochs, or with more trees
                # Run a two-stage GSCV. First stage figures out activation function, second stage figures out architecture

        return trained_final_model

    def get_relevant_categorical_rows(self, X_df, y, category):
        mask = X_df[self.categorical_column] == category

        relevant_indices = []
        relevant_y = []
        for idx, val in enumerate(mask):
            if val == True:
                relevant_indices.append(idx)
                relevant_y.append(y[idx])

        relevant_X = X_df.iloc[relevant_indices]
        # relevant_y = y[relevant_indices]

        return relevant_X, relevant_y


    def train_categorical_ensemble(self, data, categorical_column, default_category=None, **kwargs):
        self.categorical_column = categorical_column
        self.trained_category_models = {}
        self.column_descriptions[categorical_column] = 'categorical'

        self.default_category = default_category
        if self.default_category is None:
            self.search_for_default_category = True
            self.len_largest_category = 0
        else:
            self.search_for_default_category = False

        self.set_params_and_defaults(**kwargs)

        X_df, y, estimator_names = self._clean_data_and_prepare_for_training(data, self.scoring)
        X_df = X_df.reset_index(drop=True)

        print('Now fitting a single feature transformation pipeline that will be shared by all of our categorical estimators for the sake of space efficiency when saving the model')
        X_df_transformed = self.fit_transformation_pipeline(X_df, y, estimator_names[0])

        unique_categories = X_df[categorical_column].unique()

        def train_one_categorical_model(category):
            print('\n\nNow training a new estimator for the category: ' + str(category))

            relevant_X, relevant_y = self.get_relevant_categorical_rows(X_df, y, category)

            if len(relevant_X) == 0:
                return {
                    'trained_category_model': None
                    , 'category': None
                    , 'len_relevant_X': None
                }
            print('Some stats on the y values for this category: ' + str(category))
            print(pd.Series(relevant_y).describe(include='all'))

            relevant_X = self.transformation_pipeline.transform(relevant_X)


            category_trained_final_model = self.train_ml_estimator(estimator_names, self._scorer, relevant_X, relevant_y)

            self.trained_category_models[category] = category_trained_final_model

            try:
                category_length = len(relevant_X)
            except TypeError:
                category_length = relevant_X.shape[0]

            result = {
                'trained_category_model': category_trained_final_model
                , 'category': category
                , 'len_relevant_X': category_length
            }
            return result

        pool = pathos.multiprocessing.ProcessPool()

        # Since we may have already closed the pool, try to restart it
        try:
            pool.restart()
        except AssertionError as e:
            pass
        results = list(pool.map(train_one_categorical_model, unique_categories))

        # Once we have gotten all we need from the pool, close it so it's not taking up unnecessary memory
        pool.close()
        try:
            pool.join()
        except AssertionError:
            pass

        for result in results:
            if result['trained_category_model'] is not None:
                category = result['category']
                self.trained_category_models[category] = result['trained_category_model']
                if self.search_for_default_category == True and result['len_relevant_X'] > self.len_largest_category:
                    self.default_category = category
                    self.len_largest_category = result['len_relevant_X']

        print('Finished training all the category models!')

        if self.search_for_default_category == True:
            print('By default, auto_ml finds the largest category, and uses that if asked to get predictions for any rows which come from a category that was not included in the training data (i.e., if you launch a new market and ask us to get predictions for it, we will default to using your largest market to get predictions for the market that was not included in the training data')
            print('To avoid this behavior, you can either choose your own default category (the "default_category" parameter to train_categorical_ensemble), or pass in "_RAISE_ERROR" as the value for default_category, and we will raise an error when trying to get predictions for a row coming from a category that was not included in the training data.')
            print('Here is the default category we selected:')
            print(self.default_category)

        categorical_ensembler = utils_categorical_ensembling.CategoricalEnsembler(self.trained_category_models, self.transformation_pipeline, self.categorical_column, self.default_category)
        self.trained_pipeline = categorical_ensembler



    def _get_xgb_feat_importances(self, clf):

        try:
            # Handles case when clf has been created by calling
            # xgb.XGBClassifier.fit() or xgb.XGBRegressor().fit()
            fscore = clf.booster().get_fscore()
        except:
            # Handles case when clf has been created by calling xgb.train.
            # Thus, clf is an instance of xgb.Booster.
            fscore = clf.get_fscore()

        trained_feature_names = self._get_trained_feature_names()

        feat_importances = []

        # Somewhat annoying. XGBoost only returns importances for the features it finds useful.
        # So we have to go in, get the index of the feature from the "feature name" by removing the f before the feature name, and grabbing the rest of that string, which is actually the index of that feature name.
        fscore_list = [[int(k[1:]), v] for k, v in fscore.items()]


        feature_infos = []
        sum_of_all_feature_importances = 0.0

        for idx_and_result in fscore_list:
            idx = idx_and_result[0]
            # Use the index that we grabbed above to find the human-readable feature name
            feature_name = trained_feature_names[idx]
            feat_importance = idx_and_result[1]

            # If we sum up all the feature importances and then divide by that sum, we will be able to have each feature importance as it's relative feature imoprtance, and the sum of all of them will sum up to 1, just as it is in scikit-learn.
            sum_of_all_feature_importances += feat_importance
            feature_infos.append([feature_name, feat_importance])

        sorted_feature_infos = sorted(feature_infos, key=lambda x: x[1])

        print('Here are the feature_importances from the tree-based model:')
        print('The printed list will only contain at most the top 50 features.')
        for feature in sorted_feature_infos[-50:]:
            print(str(feature[0]) + ': ' + str(round(feature[1] / sum_of_all_feature_importances, 4)))


    def _print_ml_analytics_results_random_forest(self):
        try:
            final_model_obj = self.trained_final_model.named_steps['final_model']
        except:
            final_model_obj = self.trained_final_model

        print('\n\nHere are the results from our ' + final_model_obj.model_name)
        if self.name is not None:
            print(self.name)
        print('predicting ' + self.output_column)

        # XGB's Classifier has a proper .feature_importances_ property, while the XGBRegressor does not.
        if final_model_obj.model_name in ['XGBRegressor', 'XGBClassifier']:
            self._get_xgb_feat_importances(final_model_obj.model)

        else:
            trained_feature_names = self._get_trained_feature_names()

            try:
                trained_feature_importances = final_model_obj.model.feature_importances_
            except AttributeError as e:
                # There was a version of LightGBM that had this misnamed to miss the "s" at the end
                trained_feature_importances = final_model_obj.model.feature_importance_

            feature_infos = zip(trained_feature_names, trained_feature_importances)

            sorted_feature_infos = sorted(feature_infos, key=lambda x: x[1])

            print('Here are the feature_importances from the tree-based model:')
            print('The printed list will only contain at most the top 50 features.')
            for feature in sorted_feature_infos[-50:]:
                print(feature[0] + ': ' + str(round(feature[1], 4)))


    def _get_trained_feature_names(self):

        try:
            trained_feature_names = self.trained_pipeline.named_steps['dv'].get_feature_names()
        except AttributeError:
            trained_feature_names = self.transformation_pipeline.named_steps['dv'].get_feature_names()

        return trained_feature_names


    def _print_ml_analytics_results_linear_model(self):
        try:
            final_model_obj = self.trained_final_model.named_steps['final_model']
        except:
            final_model_obj = self.trained_final_model
        print('\n\nHere are the results from our ' + final_model_obj.model_name + ' model')

        trained_feature_names = self._get_trained_feature_names()

        if self.type_of_estimator == 'classifier':
            trained_coefficients = final_model_obj.model.coef_[0]
            # Note to self: this used to be accessing the [0]th index of .coef_ for classifiers. Not sure why.
        else:
            trained_coefficients = final_model_obj.model.coef_

        feature_summary = []
        for col_idx, feature_name in enumerate(trained_feature_names):
            summary_tuple = (feature_name, trained_coefficients[col_idx])
            feature_summary.append(summary_tuple)

        sorted_feature_summary = sorted(feature_summary, key=lambda x: abs(x[1]))

        print('The following is a list of feature names and their coefficients. By default, features are scaled to the range [0,1] in a way that is robust to outliers, so the coefficients are usually directly comparable to each other.')
        print('This printed list will contain at most the top 50 features.')
        for summary in sorted_feature_summary[-50:]:

            print(str(summary[0]) + ': ' + str(round(summary[1], 4)))


    def print_training_summary(self, gs):
        print('The best CV score from GridSearchCV (by default averaging across k-fold CV) for ' + self.output_column + ' is:')
        if self.took_log_of_y:
            print('    Note that this score is calculated using the natural logs of the y values.')
        print(gs.best_score_)
        print('The best params were')

        # Remove 'final_model__model' from what we print- it's redundant with model name, and is difficult to read quickly in a list since it's a python object.
        if 'model' in gs.best_params_:
            printing_copy = {}
            for k, v in gs.best_params_.items():
                if k != 'model':
                    printing_copy[k] = v
                else:
                    printing_copy[k] = utils_models.get_name_from_model(v)
        else:
            printing_copy = gs.best_params_

        print(printing_copy)

        if self.verbose:
            print('Here are all the hyperparameters that were tried:')
            raw_scores = gs.grid_scores_
            sorted_scores = sorted(raw_scores, key=lambda x: x[1], reverse=True)
            for score in sorted_scores:
                for k, v in score[0].items():
                    if k == 'model':
                        score[0][k] = utils_models.get_name_from_model(v)
                print(score)


    def predict(self, prediction_data):
        if isinstance(prediction_data, list):
            prediction_data = pd.DataFrame(prediction_data)
        prediction_data = prediction_data.copy()

        # If we are predicting a single row, we have to turn that into a list inside the first function that row encounters.
        # For some reason, turning it into a list here does not work.
        predicted_vals = self.trained_pipeline.predict(prediction_data)
        if self.took_log_of_y:
            for idx, val in predicted_vals:
                predicted_vals[idx] = math.exp(val)

        return predicted_vals


    def predict_proba(self, prediction_data):
        if isinstance(prediction_data, list):
            prediction_data = pd.DataFrame(prediction_data)
        prediction_data = prediction_data.copy()

        return self.trained_pipeline.predict_proba(prediction_data)


    def score(self, X_test, y_test, advanced_scoring=True, verbose=2):

        if isinstance(X_test, list):
            X_test = pd.DataFrame(X_test)
        y_test = list(y_test)

        X_test, y_test = utils.drop_missing_y_vals(X_test, y_test, self.output_column)

        if self._scorer is not None:
            if self.type_of_estimator == 'regressor':
                return self._scorer.score(self.trained_pipeline, X_test, y_test, self.took_log_of_y, advanced_scoring=advanced_scoring, verbose=verbose, name=self.name)

            elif self.type_of_estimator == 'classifier':
                # TODO: can probably refactor accuracy score now that we've turned scoring into it's own class
                if self._scorer == accuracy_score:
                    predictions = self.trained_pipeline.predict(X_test)
                    return self._scorer.score(y_test, predictions)
                elif advanced_scoring:
                    score, probas = self._scorer.score(self.trained_pipeline, X_test, y_test, advanced_scoring=advanced_scoring)
                    utils_scoring.advanced_scoring_classifiers(probas, y_test, name=self.name)
                    return score
                else:
                    return self._scorer.score(self.trained_pipeline, X_test, y_test, advanced_scoring=advanced_scoring)
        else:
            return self.trained_pipeline.score(X_test, y_test)


    def save(self, file_name='auto_ml_saved_pipeline.dill', verbose=True):

        # TODO: figure out how to save what could be multiple deep_learning models scattered throughout the pipeline (like for feature_learning, and eventually, ensembling)
        # Thoughts:
            # Iterate through each step
            # see if that step has a model_name, and if
        used_deep_learning = False

        # This is where we will store all of our Keras models by their name, so we can put them back in place once we've taken them out and saved the rest of the pipeline
        model_name_map = {}
        for step in self.trained_pipeline.named_steps:
            pipeline_step = self.trained_pipeline.named_steps[step]
            try:
                print('pipeline_step')
                print(pipeline_step)
                print('pipeline_step.model_name')
                print(pipeline_step.model_name)
                if pipeline_step.model_name[:12] == 'DeepLearning':
                    used_deep_learning = True

                    random_name = str(random.random())

                    keras_file_name = file_name[:-5] + random_name + '_keras_deep_learning_model.h5'

                    # Save a reference to this so we can put it back in place later
                    keras_wrapper = pipeline_step.model
                    print('keras_wrapper')
                    print(keras_wrapper)
                    model_name_map[random_name] = keras_wrapper

                    # Save the Keras model (which we have to extract from the sklearn wrapper)
                    try:
                        pipeline_step.model.save(keras_file_name)
                    except AttributeError as e:
                        # I'm not entirely clear why, but sometimes we need to access the ".model" property within a KerasRegressor or KerasClassifier, and sometimes we don't
                        pipeline_step.model.model.save(keras_file_name)

                    # Now that we've saved the keras model, set that spot in the pipeline to our random name, because otherwise we're at risk for recursionlimit errors (the model is very recursively deep)
                    # Using the random_name allows us to find the right model later if we have several (or several thousand) models to put back into place in the pipeline when we save this later
                    pipeline_step.model = random_name


                    # save this model to a .h5 file following a consistent and predictable naming schema
                        # Probably using the step name we're iterating through
                    # remove this model property from the trained pipeline
            except AttributeError as e:
                print('AttributeError while trying to see if we should save the keras model')
                print(e)
                pass

        if used_deep_learning == True:
            # Save the rest of the pipeline without the deep learning models
            with open(file_name, 'wb') as open_file_name:
                dill.dump(self.trained_pipeline, open_file_name)

            # Now add the deep learning models back in so we can use the pipeline that's already loaded in memory, rather than having to load back in the saved pipeline
            for step in self.trained_pipeline.named_steps:
                pipeline_step = self.trained_pipeline.named_steps[step]
                try:
                    if pipeline_step.model_name[:12] == 'DeepLearning':

                        # This is where we saved the random_name for this model
                        random_name = pipeline_step.model
                        model = model_name_map[random_name]

                        # Put the model back in place so that we can still use it to get predictions without having to load it back in from disk
                        pipeline_step.model = model
                except AttributeError:
                    pass

            # Save the whole pipeline
            # Lots of logging
            # Then we have to figure out how to load it (ideally in a way that's compatible with ensembles)

            # TODO: user logging
            if verbose:
                print('\n\nSaved the Keras model to it\'s own file(s). For instance:')
                print(keras_file_name)
                print('To load the entire trained pipeline with the Keras deep learning model from disk, we will need to load it specifically using a dedicated function in auto_ml:\n\n')
                print('from auto_ml.utils_models import load_keras_model')
                print('trained_ml_pipeline = load_keras_model(' + file_name + ')')
                print('\nIt is also important to keep all files auto_ml needs in the same directory. If you transfer this to a different prod machine, be sure to transfer both/all of these files, and keep the same name:')
                print(file_name)
                print('And all of the Keras files, such as:')
                print(keras_file_name)


        else:
            # Perform logic for non-keras models here


        # is_deep_learning = False
        # try:
        #     if self.trained_pipeline.named_steps['final_model'].model_name[:12] == 'DeepLearning':
        #         is_deep_learning = True
        # except:
        #     pass

        # if is_deep_learning == True:
        #     keras_file_name = file_name[:-5] + '_keras_deep_learning_model.h5'

        #     keras_wrapper = self.trained_pipeline.named_steps['final_model'].model
        #     self.trained_pipeline.named_steps['final_model'].model.model.save(keras_file_name)

        #     # Now that we've saved the keras model, set that spot in the pipeline to None, because otherwise we're at risk for recursionlimit errors (the model is very recursively deep)
        #     self.trained_pipeline.named_steps['final_model'].model = None
        #     with open(file_name, 'wb') as open_file_name:
        #         dill.dump(self.trained_pipeline, open_file_name)

        #     # Put the model back in place so that we can still use it to get predictions without having to load it back in from disk
        #     self.trained_pipeline.named_steps['final_model'].model = keras_wrapper
        #     if verbose:
        #         print('\n\nSaved the Keras model to it\'s own file:')
        #         print(keras_file_name)
        #         print('To load the entire trained pipeline with the Keras deep learning model from disk, we will need to load it specifically using a dedicated function in auto_ml:\n\n')
        #         print('from auto_ml.utils_models import load_keras_model')
        #         print('trained_ml_pipeline = load_keras_model(' + file_name + ')')
        #         print('\nIt is also important to keep both files auto_ml needs in the same directory. If you transfer this to a different prod machine, be sure to transfer both of these files, and keep the same name:')
        #         print(file_name)
        #         print(keras_file_name)

        # else:
            with open(file_name, 'wb') as open_file_name:
                dill.dump(self.trained_pipeline, open_file_name)

            if verbose:
                print('\n\nWe have saved the trained pipeline to a filed called "' + file_name + '"')
                print('It is saved in the directory: ')
                print(os.getcwd())
                print('To use it to get predictions, please follow the following flow (adjusting for your own uses as necessary:\n\n')
                print('`with open("' + file_name + '", "rb") as read_file:`')
                print('`    trained_ml_pipeline = dill.load(read_file)`')
                print('`trained_ml_pipeline.predict(list_of_dicts_with_same_data_as_training_data)`\n\n')

                print('Note that this pickle/dill file can only be loaded in an environment with the same modules installed, and running the same Python version.')
                print('This version of Python is:')
                print(sys.version_info)

                print('\n\nWhen passing in new data to get predictions on, columns that were not present (or were not found to be useful) in the training data will be silently ignored.')
                print('It is worthwhile to make sure that you feed in all the most useful data points though, to make sure you can get the highest quality predictions.')

        return os.path.join(os.getcwd(), file_name)
