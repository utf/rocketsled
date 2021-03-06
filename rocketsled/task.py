"""
The FireTask for running automatic optimization loops.

Please see the documentation for a comprehensive guide on usage. 
"""
import pickle
import warnings
import random
import heapq
from time import sleep
from os import getpid, path
from functools import reduce
from itertools import product
from collections.abc import Iterable

import numpy as np
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor, \
    ExtraTreesRegressor
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.preprocessing import LabelBinarizer
from sklearn.preprocessing import StandardScaler
from fireworks.utilities.fw_utilities import explicit_serialize
from fireworks.core.firework import FireTaskBase
from fireworks import FWAction, LaunchPad

from rocketsled.acq import acquire
from rocketsled.utils import deserialize, Dtypes, pareto, \
    convert_value_to_native, split_xz

__author__ = "Alexander Dunn"
__email__ = "ardunn@lbl.gov"


@explicit_serialize
class OptTask(FireTaskBase):
    """
    A FireTask for automatically running optimization loops and storing
    optimization data for complex workflows.

    OptTask takes in _x and _y from the fw_spec (input/output of
    current guess), gathers X (previous guesses input) and y (previous guesses
    output), and predicts the next best guess.

    Required args:
        wf_creator (str): Module path to a function that returns a workflow
            based on a unique vector, x.
        dimensions ([tuple]): each 2-tuple in the list defines one dimension in
            the search space in (low, high) format.
            For categorical or discontinuous dimensions, includes all possible
            categories or values as a list of any length or a tuple of length>2.
            Example: dimensions = dim = [(1,100), (9.293, 18.2838), ("red",
            "blue", "green")].
            
    Optional args:
    
        Database setup:
        lpad (LaunchPad): A Fireworks LaunchPad object, which can be used to
            define the host/port/name of the db.
        opt_label (string): Names the collection of that the particular
            optimization's data will be stored in. Multiple collections
            correspond to multiple independent optimizations.
    
        Predictors:
        predictor (string): names a function which given a list of explored
            points and unexplored points, returns an optimized guess.
            Included sklearn predictors are:
                'GaussianProcessRegressor',
                'RandomForestRegressor',
                'ExtraTreesRegressor',
                'GradientBoostingRegressor',
            To use a random guess, use 'random'
            Defaults to 'GaussianProcess'
            Example builtin predictor: predictor = 'GaussianProcessRegressor'
            Example custom predictor: predictor = 'my_module.my_predictor'
        predictor_args (list): the positional args to be passed to the model
            along with a list of points to be searched. For sklearn-based
            predictors included in OptTask, these positional args are passed to
            the init method of the chosen model. For custom predictors, these
            are passed to the chosen predictor function alongside the searched
            guesses, the output from searched guesses, and an unsearched space
            to be used with optimization.
        predictor_kwargs (dict): the kwargs to be passed to the model. Similar
            to predictor_args.
        
        Predictor performance:
        n_searchpts (int): The number of points to be searched in the search
            space when choosing the next best point. Choosing more points to
            search may increase the effectiveness of the optimization but take
            longer to evaluate. The default is 1000 points.
        n_trainpts (int): The number of already explored points to be chosen
            for training. Default is None, meaning all available points will be
            used for training. Reduce the number of points to decrease training
            times.
        space_file (str): The fully specified path of a pickle file containing a
            list of all possible searchable vectors.
            For example '/Users/myuser/myfolder/myspace.p'. When loaded, this
            space_file should be a list of tuples.
        acq (str): The acquisition function to use. Can be 'ei' for expected
            improvement, 'pi' for probability of improvement, or 'lcb' for lower
            confidence bound. Defaults to None, which means no acquisition
            function is used, and the highest predicted point is picked
            (greedy algorithm). Only applies to builtin predictors.
        n_boostraps (int): The number of times each optimization should, sample,
            train, and predict values when generating uncertainty estimates for
            prediction. Only used if acq specified. At least 10 data points must
            be present for bootstrapping.

        Features:
        get_z (string): the fully-qualified name of a function which, given a x
            vector, returns another vector z which provides extra information
            to the machine learner. The features defined in z are not used to
            run the workflow, but are used for learning. If z_features are
            enabled, ONLY z features will be used for learning (x vectors
            essentially become tags or identifiers only).
            Examples: 
                get_z = 'my_module.my_fun'
                get_z = '/path/to/folder/containing/my_package/my_module.my_fun'
        get_z_args (list): the positional arguments to be passed to the get_z
            function alongside x
        get_z_kwargs (dict): the kwargs to be passed to the get_z function
            alongside x
        persistent_z (str): The filename (pickle file) which should be used to
            store persistent z calculations. Specify this argument if
            calculating z for many (n_search_points) is not trivial and will
            cost time in computing. With this argument specified, each z will
            only be calculated once. Defaults to None, meaning that all
            unexplored z are re-calculated each iteration.
            Example:
                persistent_z = '/path/to/persistent_z_guesses.p'
        
        Miscellaneous:
        wf_creator_args (list): the positional args to be passed to the
            wf_creator function alongsize the new x vector
        wf_creator_kwargs (dict): details the kwargs to be passed to the
            wf_creator function alongside the new x vector
        encode_categorical (bool): If True, preprocesses categorical data
            (strings) to one-hot encoded binary arrays for use with custom
            predictor functions. Default False.
        duplicate_check (bool): If True, checks that custom optimizers are not
            making duplicate guesses; all built-in optimizers cannot duplicate
            guess. If the custom predictor suggests a duplicate, OptTask picks
            a random guess out of the remaining untried space. Default is no
            duplicate check, and an error is raised if a duplicate is suggested.
        tolerances (list): The tolerance of each feature when duplicate
            checking. For categorical features, put 'None'
            Example: Our dimensions are [(1, 100), ['red', 'blue'],
            (2.0, 20.0)]. We want our first parameter to be  a duplicate only
            if it is exact, and our third parameter to be a duplicate if it is
            within 1e-6. Then:
                tolerances=[0, None, 1e-6]
        maximize (bool): If true, makes optimization tend toward maximum values
            instead of minimum ones.
        batch_size (int): The number of jobs to submit per batch for a batch
            optimization. For example, batch_size=5 will optimize every 5th job,
            then submit another 5 jobs based on the best 5 predictions.
        enforce_sequential (bool): WARNING: Experimental feature! If True,
            enforces that RS optimizations are run sequentially (default), which
            prevents duplicate guesses from ever being run. If False, allows
            OptTasks to run optimizations in parallel, which may cause duplicate
            guesses with high parallelism.
        timeout (int): The number of seconds to wait before resetting the lock
            on the db.
    """
    _fw_name = "OptTask"
    required_params = ['wf_creator', 'dimensions']
    optional_params = ['lpad', 'opt_label', 'predictor', 'predictor_args',
                       'predictor_kwargs', 'n_search_points', 'n_train_points',
                       'acq', 'space_file', 'get_z', 'get_z_args',
                       'get_z_kwargs', 'wf_creator_args', 'wf_creator_kwargs',
                       'encode_categorical', 'duplicate_check', 'max',
                       'batch_size', 'tolerance', 'timeout', 'n_boostraps',
                       'enforce_sequential']

    def __init__(self, *args, **kwargs):
        # instance attrs to be inserted here...
        super(OptTask, self).__init__(*args, **kwargs)

    def run_task(self, fw_spec):
        """
        FireTask for running an optimization loop.

        Args:
            fw_spec (dict): the firetask spec. Must contain a '_y' key with
            a float type field and must contain a '_x' key containing a
            vector uniquely defining the point in search space.

        Returns:
            (FWAction) A workflow based on the workflow creator and a new,
            optimized guess.
        """
        self.enforce_sequential = self.get("enforce_sequential", True)
        pid = getpid()
        sleeptime = .01
        timeout = self['timeout'] if 'timeout' in self else 500
        max_runs = int(timeout / sleeptime)
        max_resets = 3
        self._setup_db(fw_spec)

        # points for which a workflow has already been run
        self._completed = {'x': {'$exists': 1}, 'y': {'$exists': 1, '$ne':
            'reserved'}, 'z': {'$exists': 1}}
        # the query format for the manager document
        self._manager = {'lock': {'$exists': 1}, 'queue': {'$exists': 1}}

        # Running stepwise optimization for concurrent processes requires a
        # manual 'lock' on the optimization database to prevent duplicate
        # guesses. The first process sets up a manager document which handles
        # locking and queueing processes by PID. The single, active process in
        # the lock is free to access optimization data; the queue of the manager
        # holds parallel process PIDs waiting to access the db. When the active
        # process finishes, it removes itself from the lock and moves the first
        # queue PID into the lock, allowing the next process to begin
        # optimization. Each process continually tries to either queue or place
        # itself into the lock if not active.

        for run in range(max_resets * max_runs):
            manager_count = self.c.count_documents(self._manager)

            if manager_count == 0:
                self.c.insert_one({'lock': pid, 'queue': []})
            elif manager_count == 1:

                # avoid bootup problems if manager lock is being deleted
                # concurrently with this check
                try:
                    manager = self.c.find_one(self._manager)
                    manager_id = manager['_id']
                    lock = manager['lock']

                except TypeError:
                    continue

                if lock is None:
                    self.c.find_one_and_update({'_id': manager_id},
                                               {'$set': {'lock': pid}})

                elif self.enforce_sequential and lock != pid:
                    if pid not in manager['queue']:

                        # avoid bootup problems if manager queue is being
                        # deleted concurrently with this check
                        try:
                            self.c.find_one_and_update({'_id': manager_id},
                                                       {'$push': {'queue': pid}}
                                                       )
                        except TypeError:
                            continue
                    else:
                        sleep(sleeptime)

                elif not self.enforce_sequential or \
                        (self.enforce_sequential and lock == pid):
                    try:
                        x, y, z, x_dims, XZ_new, predictor, n_completed = \
                            self.optimize(fw_spec, manager_id)
                    except BatchNotReadyError:
                        return None
                    except Exception:
                        self.pop_lock(manager_id)
                        raise

                    # make sure a process has not timed out and changed the lock
                    # pid while this process is computing the next guess
                    try:
                        if self.c.find_one(self._manager)['lock'] != pid or \
                                self.c.count_documents(self._manager) == 0:
                            continue
                        else:
                            opt_id = self.stash(x, y, z, x_dims, XZ_new,
                                                predictor, n_completed)

                    except TypeError as E:
                        warnings.warn("Process {} probably timed out while "
                                      "computing next guess, with exception {}."
                                      " Try shortening the training time or "
                                      "lengthening the timeout for OptTask!"
                                      "".format(pid, E), RuntimeWarning)
                        raise E
                        # continue

                    self.pop_lock(manager_id)
                    X_new = [split_xz(xz_new, x_dims, x_only=True) for
                             xz_new in XZ_new]
                    wf_creator = deserialize(self['wf_creator'])
                    wf_creator_args = self.get('wf_creator_args', [])
                    wf_creator_kwargs = self.get('wf_creator_kwargs', {})

                    if not isinstance(wf_creator_args, (list, tuple)):
                        raise TypeError(
                            "wr_creator_args should be a list/tuple of "
                            "positional arguments.")

                    if not isinstance(wf_creator_kwargs, dict):
                        raise TypeError(
                            "wr_creator_kwargs should be a dictionary of "
                            "keyword arguments.")

                    new_wfs = [wf_creator(x_new, *wf_creator_args,
                                          **wf_creator_kwargs)
                               for x_new in X_new]
                    for wf in new_wfs:
                        self.lpad.add_wf(wf)
                    return FWAction(update_spec={'_optimization_id': opt_id},
                                    stored_data={'_optimization_id': opt_id})
            else:
                # Delete the manager that this has created
                self.c.delete_one({'lock': pid})

            if run in [max_runs * k for k in range(1, max_resets)]:
                self.c.find_one_and_update(self._manager,
                                           {'$set': {'lock': None, 'queue': []}}
                                           )

            elif run == max_runs * max_resets:
                raise Exception("The manager is still stuck after "
                                "resetting. Make sure no stalled processes "
                                "are in the queue.")

    def optimize(self, fw_spec, manager_id):
        """
        Run the optimization algorithm.

        Args:
            fw_spec (dict): The firework spec.
            manager_id (ObjectId): The MongoDB object id of the manager
                document.

        Returns:
            x (iterable): The current x guess.
            y: (iterable): The current y (objective function) value
            z (iterable): The z vector associated with x
            x_dims ([list] or [tuple]): The dimensions of the domain
            XZ_new ([list] or [tuple]): The predicted next best guess(es),
                including their associated z vectors
            predictor (str): The name of the predictor used to make XZ_new
            n_completed (int): The number of completed guesses/workflows
        """
        # required args
        x_dims = self['dimensions']

        # predictor definition
        predictor = self.get('predictor', 'RandomForestRegressor')
        predargs = self.get('predictor_args', [])
        predkwargs = self.get('predictor_kwargs', {})

        # predictor performance
        n_trainpts = self.get('n_trainpts', None)
        self.n_searchpts = self.get('n_searchpts', 1000)
        if 'acq' in self:
            self.acq = self['acq']
            acq_funcs = [None, 'ei', 'pi', 'lcb', 'maximin']
            if self.acq not in acq_funcs:
                raise ValueError(
                    "Invalid acquisition function. Use 'ei', 'pi', 'lcb', "
                    "'maximin' (multiobjective), or None.")
        else:
            self.acq = None
        self.n_boostraps = self.get('n_boostraps', 500)

        # extra features
        if 'get_z' in self and self['get_z'] is not None:
            self.get_z = deserialize(self['get_z'])
        else:
            self.get_z = lambda *args, **kwargs: []
        get_z_args = self.get('get_z_args', [])
        get_z_kwargs = self.get('get_z_kwargs', {})
        persistent_z = self.get('persistent_z', None)

        # miscellaneous
        encode_categorical = self.get('encode_categorical', False)
        duplicate_check = self.get('duplicate_check', False)
        tolerances = self.get('tolerances', None)
        maximize = self.get('maximize', False)
        batch_size = self.get('batch_size', 1)

        for kwname, kwdict in \
                {'get_z_kwargs': get_z_kwargs,
                 'predictor_kwargs': predkwargs}.items():
            if not isinstance(kwdict, dict):
                raise TypeError("{} should be a dictonary of keyword arguments."
                                "".format(kwname))

        for argname, arglist in \
                {'get_z_args': get_z_args,
                 'predictor_args': predargs}.items():
            if not isinstance(arglist, (list, tuple)):
                raise TypeError("{} should be a list/tuple of positional "
                                "arguments".format(argname))

        x = list(fw_spec['_x'])
        y = fw_spec['_y']

        if isinstance(y, (list, tuple)):
            if len(y) == 1:
                y = y[0]

            self.n_objs = len(y)
            if self.acq not in ("maximin", None):
                raise ValueError(
                    "{} is not a valid acquisition function for multiobjective "
                    "optimization".format(self.acq))
        else:
            if self.acq == "maximin":
                raise ValueError(
                    "Maximin is not a valid acquisition function for single "
                    "objective optimization.")
            self.n_objs = 1

        # If process A suggests a certain guess and runs it,
        # process B may suggest the same guess while process A
        # is running its new workflow. Therefore, process A must
        # reserve the guess. Line below releases reservation on
        # this document in case of workflow failure or end of
        # workflow.
        self.c.delete_one({'x': x, 'y': 'reserved'})
        self.dtypes = Dtypes()
        self._xdim_spec = self._check_dims(x_dims)

        # fetch additional attributes for constructing ML model
        z = self.get_z(x, *get_z_args, **get_z_kwargs)

        # use all possible training points as default
        n_completed = self.c.count_documents(self._completed)
        if not n_trainpts or n_trainpts > n_completed:
            n_trainpts = n_completed

        # check if opimization should be done, if in batch mode
        batch_mode = False if batch_size == 1 else True
        batch_ready = n_completed not in (0, 1) and \
                      (n_completed + 1) % batch_size == 0

        x = self._convert_native(x)
        y = self._convert_native(y)
        z = self._convert_native(z)

        if batch_mode and not batch_ready:
            # 'None' predictor means this job was not used for
            # an optimization run.
            if self.c.find_one({'x': x}):
                if self.c.find_one({'x': x, 'y': 'reserved'}):
                    # For reserved guesses: update everything
                    self.c.find_one_and_update(
                        {'x': x, 'y': 'reserved'},
                        {'$set': {'y': y, 'z': z, 'z_new': [],
                                  'x_new': [],
                                  'predictor': None,
                                  'index': n_completed + 1}
                         })
                else:
                    # For completed guesses (ie, this workflow
                    # is a forced duplicate), do not update
                    # index, but update everything else
                    self.c.find_one_and_update(
                        {'x': x},
                        {'$set': {'y': y, 'z': z, 'z_new': [], 'x_new': [],
                                  'predictor': None}
                         })
            else:
                # For new guesses: insert x, y, z, index,
                # predictor, and dummy new guesses
                self.c.insert_one({'x': x, 'y': y, 'z': z, 'x_new': [],
                                   'z_new': [], 'predictor': None,
                                   'index': n_completed + 1})
            self.pop_lock(manager_id)
            raise BatchNotReadyError

        # Mongo aggregation framework may give duplicate documents, so we cannot
        # use $sample to randomize the training points used
        explored_indices = random.sample(
            range(1, n_completed + 1), n_trainpts)
        explored_docs = self.c.find(
            {'index': {'$in': explored_indices}},
            batch_size=10000)
        reserved_docs = self.c.find({'y': 'reserved'}, batch_size=10000)
        reserved = []
        for doc in reserved_docs:
            reserved.append(doc['x'])
        Y = [None] * n_completed
        Y.append(y)
        X_explored = [None] * n_completed
        X_explored.append(x)
        z = list(z)
        XZ_explored = [None] * n_completed
        XZ_explored.append(x + z)
        for i, doc in enumerate(explored_docs):
            X_explored[i] = doc['x']
            XZ_explored[i] = doc['x'] + doc['z']
            Y[i] = doc['y']

        X_space = self._discretize_space(x_dims)
        X_space = list(X_space) if persistent_z else X_space
        X_unexplored = []
        for xi in X_space:
            xj = list(xi)
            if xj not in X_explored and xj not in reserved:
                X_unexplored.append(xj)
                if len(X_unexplored) == self.n_searchpts:
                    break

        if persistent_z:
            if path.exists(persistent_z):
                with open(persistent_z, 'rb') as f:
                    xz_map = pickle.load(f)
            else:
                xz_map = {tuple(xi): self.get_z(xi, *get_z_args, **get_z_kwargs)
                          for xi in X_space}
                with open(persistent_z, 'wb') as f:
                    pickle.dump(xz_map, f)

            XZ_unexplored = [xi + xz_map[tuple(xi)] for xi in X_unexplored]
        else:
            XZ_unexplored = [xi + self.get_z(xi, *get_z_args, **get_z_kwargs)
                             for xi in X_unexplored]

        # if there are no more unexplored points in the entire
        # space, either they have been explored (ie have x, y,
        # and z) or have been reserved.
        if len(XZ_unexplored) < 1:
            if self._is_discrete(x_dims, criteria='all'):
                raise ExhaustedSpaceError("The discrete space has been searched"
                                          " exhaustively.")
            else:
                raise TypeError("A comprehensive list of points was exhausted "
                                "but the dimensions are not discrete.")
        z_dims = self._z_dims(XZ_unexplored, len(x_dims))
        xz_dims = x_dims + z_dims

        # run machine learner on Z or X features
        plist = [RandomForestRegressor, GaussianProcessRegressor,
                 ExtraTreesRegressor, GradientBoostingRegressor]
        self.predictors = {p.__name__: p for p in plist}
        if predictor in self.predictors:
            model = self.predictors[predictor]
            XZ_explored = self._encode(XZ_explored, xz_dims)
            XZ_unexplored = self._encode(XZ_unexplored, xz_dims)
            XZ_onehot = []
            for _ in range(batch_size):
                xz1h = self._predict(XZ_explored, Y, XZ_unexplored,
                                     model(*predargs, **predkwargs),
                                     maximize, scaling=True)
                ix = XZ_unexplored.index(xz1h)
                XZ_unexplored.pop(ix)
                XZ_onehot.append(xz1h)

            XZ_new = [self._decode(xz_onehot, xz_dims) for
                      xz_onehot in XZ_onehot]

        elif predictor == 'random':
            XZ_new = random.sample(XZ_unexplored, batch_size)

        else:
            # If using a custom predictor, automatically convert
            # categorical info to one-hot encoded ints.
            # Used when a custom predictor cannot natively use
            # categorical info
            if encode_categorical:
                XZ_explored = self._encode(XZ_explored, xz_dims)
                XZ_unexplored = self._encode(XZ_unexplored, xz_dims)

            try:
                predictor_fun = deserialize(predictor)
            except Exception as E:
                raise NameError("The custom predictor {} didnt import "
                                "correctly!\n{}".format(predictor, E))

            XZ_new = predictor_fun(XZ_explored, Y, x_dims, XZ_unexplored,
                                   *predargs, **predkwargs)
            if not isinstance(XZ_new[0], (list, tuple)):
                XZ_new = [XZ_new]

        # duplicate checking for custom optimizer functions
        if duplicate_check:

            if not self.enforce_sequential:
                raise ValueError("Duplicate checking cannot work when "
                                 "optimizations are not enforced sequentially.")

            # todo: fix batch_mode duplicate checking
            if batch_mode:
                raise Exception("Dupicate checking in batch mode for custom "
                                "predictors is not yet supported")

            if predictor not in self.predictors and predictor != 'random':
                X_new = [split_xz(xz_new, x_dims, x_only=True) for
                         xz_new in XZ_new]
                X_explored = [split_xz(xz, x_dims, x_only=True) for
                              xz in XZ_explored]

                if tolerances:
                    for n, x_new in enumerate(X_new):
                        if self._tolerance_check(
                                x_new, X_explored,
                                tolerances=tolerances):
                            XZ_new[n] = random.choice(
                                XZ_unexplored)
                else:
                    if self._is_discrete(x_dims):
                        # test only for x, not xz because custom predicted z
                        # may not be accounted for
                        for n, x_new in enumerate(X_new):
                            if x_new in X_explored or x_new == x:
                                XZ_new[n] = random.choice(
                                    XZ_unexplored)
                    else:
                        raise ValueError("Define tolerances parameter to "
                                         "duplicate check floats.")
        return x, y, z, x_dims, XZ_new, predictor, n_completed

    def stash(self, x, y, z, x_dims, XZ_new, predictor, n_completed):
        """
        Write documents to database after optimization.

        Args:
            x (iterable): The current x guess.
            y: (iterable): The current y (objective function) value
            z (iterable): The z vector associated with x
            x_dims ([list] or [tuple]): The dimensions of the domain
            XZ_new ([list] or [tuple]): The predicted next best guess(es),
                including their associated z vectors
            predictor (str): The name of the predictor used to make XZ_new
            n_completed (int): The number of completed guesses/workflows

        Returns:
            opt_id (pymongo InsertedOneResult): The result of the insertion
                of the new optimization document in the database. If multiple
                opt_ids are valid (ie batch mode is enabled), the last opt_id
                is returned.
        """

        for xz_new in XZ_new:
            # separate 'predicted' z features from the new x vector
            x_new, z_new = split_xz(xz_new, x_dims)
            x_new = self._convert_native(x_new)
            z_new = self._convert_native(z_new)

            # if it is a duplicate (such as a forced
            # identical first guess)
            forced_dupe = self.c.find_one({'x': x})

            acqmap = {"ei": "Expected Improvement",
                      "pi": "Probability of Improvement",
                      "lcb": "Lower Confidence Boundary",
                      None: "Highest Value",
                      "maximin": "Maximin Expected "
                                 "Improvement"}
            if predictor in self.predictors:
                predictorstr = predictor + " with acquisition: " + acqmap[
                    self.acq]
                if self.n_objs > 1:
                    predictorstr += " using {} objectives".format(self.n_objs)
            else:
                predictorstr = predictor
            if forced_dupe:
                # only update the fields which should be updated
                self.c.find_one_and_update(
                    {'x': x},
                    {'$set': {'y': y, 'z': z,
                              'z_new': z_new,
                              'x_new': x_new,
                              'predictor': predictorstr}
                     })
                opt_id = forced_dupe['_id']
            else:
                # update all the fields, as it is a new document
                res = self.c.insert_one(
                    {'z': z, 'y': y, 'x': x, 'z_new': z_new, 'x_new': x_new,
                     'predictor': predictorstr, 'index': n_completed + 1})
                opt_id = res.inserted_id

            # ensure previously fin. workflow results are not overwritten by
            # concurrent predictions
            if self.c.count_documents(
                    {'x': x_new, 'y': {'$exists': 1, '$ne': 'reserved'}}) == 0:
                # reserve the new x to prevent parallel processes from
                # registering it as unexplored, since the next iteration of this
                # process will be exploring it
                res = self.c.insert_one({'x': x_new, 'y': 'reserved'})
                opt_id = res.inserted_id
            else:
                raise ValueError(
                    "The predictor suggested a guess which has already been "
                    "tried: {}".format(x_new))
        return opt_id

    def _setup_db(self, fw_spec):
        """
        Sets up a MongoDB database for storing optimization data.

        Args:
            fw_spec (dict): The spec of the Firework which contains this
                Firetask.

        Returns:
            None
        """
        if 'opt_label' in self:
            opt_label = self['opt_label']
        else:
            opt_label = 'opt_default'

        if 'lpad' in self:
            lpad_dict = self['lpad']
            lpad = LaunchPad.from_dict(lpad_dict)

        elif '_add_launchpad_and_fw_id' in fw_spec and \
                fw_spec['_add_launchpad_and_fw_id']:
            lpad = self.launchpad
        else:
            try:
                lpad = LaunchPad.auto_load()

            except AttributeError:
                # auto_load did not return any launchpad object, so nothing was
                # defined.
                raise AttributeError("The optimization database must be "
                                     "specified explicitly with a Launchpad "
                                     "object (lpad), by setting "
                                     "_add_launchpad_and_fw_id to "
                                     "True in the fw_spec, or by defining "
                                     "LAUNCHPAD_LOC in your config file for "
                                     "LaunchPad.auto_load()")
        self.lpad = lpad
        self.c = getattr(self.lpad.db, opt_label)

    def _check_dims(self, dims):
        """
        Ensure the dimensions are in the correct format for the optimization.
        
        Dimensions should be a list or tuple of lists or tuples each defining
        the search space in one dimension. The datatypes used inside each
        dimension's  definition should be NumPy compatible datatypes.

        Continuous numerical dimensions (floats and ranges of ints) should be
        2-tuples in the form (upper, lower). Categorical dimensions or
        discontinuous numerical dimensions should be exhaustive lists/tuples
        such as ['red', 'green', 'blue'] or [1.2, 11.5, 15.0].
        
        Args:
            dims (list): The dimensions of the search space. 

        Returns:
            [str]: Types of the dimensions in the search space defined by dims.
        """
        dims_types = [list, tuple]
        dim_spec = []

        if type(dims) not in dims_types:
            raise TypeError("The dimensions must be a list or tuple.")

        for dim in dims:
            if type(dim) not in dims_types:
                raise TypeError("The dimension {} must be a list or tuple."
                                "".format(dim))

            for entry in dim:
                if type(entry) not in self.dtypes.all:
                    raise TypeError("The entry {} in dimension {} cannot be "
                                    "used with OptTask. A list of acceptable "
                                    "datatypes is {}".format(entry, dim,
                                                             self.dtypes.all))
                for dset in [self.dtypes.ints,
                             self.dtypes.floats,
                             self.dtypes.others]:
                    if type(entry) not in dset and type(dim[0]) in dset:
                        raise TypeError(
                            "The dimension {} contains heterogeneous"
                            " types: {} and {}".format(dim,
                                                       type(dim[0]),
                                                       type(entry)))
            if isinstance(dim, list):
                if type(dim[0]) in self.dtypes.ints:
                    dim_spec.append("int_set")
                elif type(dim[0]) in self.dtypes.floats:
                    dim_spec.append("float_set")
                elif type(dim[0]) in self.dtypes.others:
                    dim_spec.append("categorical {}".format(len(dim)))
            elif isinstance(dim, tuple):
                if type(dim[0]) in self.dtypes.ints:
                    dim_spec.append("int_range")
                elif type(dim[0]) in self.dtypes.floats:
                    dim_spec.append("float_range")
                elif type(dim[0]) in self.dtypes.others:
                    dim_spec.append("categorical {}".format(len(dim)))
        return dim_spec

    def _is_discrete(self, dims, criteria='all'):
        """
        Checks if the search space is discrete.

        Args:
            dims ([tuple]): dimensions of the search space
            criteria (str/unicode): If 'all', returns bool based on whether
                ALL dimensions are discrete. If 'any', returns bool based on
                whether ANY dimensions are discrete.

        Returns:
            (bool) whether the search space is totally discrete.
        """

        if criteria == 'all':
            for dim in dims:
                if type(dim[0]) not in self.dtypes.discrete or \
                        type(dim[1]) not in self.dtypes.discrete:
                    return False
            return True

        elif criteria == 'any':
            for dim in dims:
                if type(dim[0]) in self.dtypes.discrete or \
                        type(dim[1]) in self.dtypes.discrete:
                    return True
            return False

    def _discretize_space(self, dims, n_floats=100):
        """
        Create a list of points for searching during optimization. 

        Args:
            dims ([tuple]): dimensions of the search space.
            n_floats (int): Number of floating points to sample from each
                continuous dimension when discrete dimensions are present. If
                all dimensions are continuous, this argument is ignored and
                a space of n_searchpts is generated in a more efficient manner.

        Returns:
            ([list]) Points of the search space. 
        """
        if 'space_file' in self:
            if self['space_file']:
                with open(self['space_file'], 'rb') as f:
                    return pickle.load(f)

        # Ensure consistency of dimensions
        for dim in dims:
            if isinstance(dim, tuple) and len(dim) == 2:
                for dtype in ['ints', 'floats']:
                    if type(dim[0]) not in getattr(self.dtypes, dtype) != \
                            type(dim[1]) not in getattr(self.dtypes, dtype):
                        raise ValueError("Ranges of values for dimensions "
                                         "must be the same general datatype,"
                                         "not ({}, {}) for {}"
                                         "".format(type(dim[0]),
                                                   type(dim[1]), dim))

        dims_ranged = all([len(dim) == 2 for dim in dims])
        dims_float = all([type(dim[0]) in self.dtypes.floats for dim in dims])
        if dims_float and dims_ranged:
            # Save computation/memory if all ranges of floats
            nf = self.n_searchpts
            space = np.zeros((nf, len(dims)))
            for i, dim in enumerate(dims):
                low = dim[0]
                high = dim[1]
                space[:, i] = np.random.uniform(low=low, high=high, size=nf)
            return space.tolist()
        else:
            # todo: this could be faster
            total_dimspace = []
            for dim in dims:
                if isinstance(dim, (tuple, list)) and len(dim) == 2:
                    low = dim[0]
                    high = dim[1]
                    if type(low) in self.dtypes.ints:
                        # Then the dimension is of the form (low, high)
                        dimspace = list(range(low, high + 1))
                    elif type(low) in self.dtypes.floats:
                        dimspace = np.random.uniform(low=low, high=high,
                                                     size=n_floats).tolist()
                    else:  # The dimension is a 2-tuple of strings
                        dimspace = dim
                else:  # the dimension is a list of entries
                    dimspace = dim
                random.shuffle(dimspace)
                total_dimspace.append(dimspace)
            if len(dims) == 1:
                return [[xi] for xi in total_dimspace[0]]
            else:
                return product(*total_dimspace)

    def pop_lock(self, manager_id):
        """
        Releases the current process lock on the manager doc, and moves waiting
        processes from the queue to the lock.

        Args:
            manager_id: The MongoDB ObjectID object of the manager doc.

        Returns:
            None
        """
        queue = self.c.find_one({'_id': manager_id})['queue']
        if not queue:
            self.c.find_one_and_update({'_id': manager_id},
                                       {'$set': {'lock': None}})
        else:
            new_lock = queue.pop(0)
            self.c.find_one_and_update({'_id': manager_id},
                                       {'$set': {'lock': new_lock,
                                                 'queue': queue}})

    def _predict(self, X, Y, space, model, maximize, scaling):
        """
        Scikit-learn compatible model for stepwise optimization. It uses a
        regressive predictor evaluated on remaining points in a discrete space.

        Since sklearn modules cannot deal with categorical data, categorical
        data is preprocessed by _encode before being passed to _predict,
        and predicted x vectors are postprocessed by _decode to convert to
        the original categorical dimensions.

        Args:
            X ([list]): List of vectors containing input training data.
            Y (list): List of scalars containing output training data. Can
                be a list of vectors if undergoing multiobjective optimization.
            space ([list]): List of vectors containing all unexplored inputs.
                Should be preprocessed.
            model (sklearn model): The regressor used for predicting the next
                best guess.
            maximize (bool): Makes predictor return the guess which maximizes
                the predicted objective function output. Else minmizes the
                predicted objective function output.
            scaling (bool): If True, scale data with StandardScaler (required
                for some optimizers, such as Gaussian processes).

        Returns:
            (list) A vector which is predicted to minimize (or maximize) the
                objective function.
        """

        # Scale data if all floats for dimensions in question
        if scaling:
            scaler = StandardScaler()
            train_set = np.vstack((X, space))
            scaler.fit(train_set)
            X_scaled = scaler.transform(X)
            space_scaled = scaler.transform(space)
        else:
            X_scaled = X
            space_scaled = space

        n_explored = len(X)
        n_unexplored = len(space)

        # If get_z defined, only use z features!
        if 'get_z' in self:
            encoded_xlen = 0
            for t in self._xdim_spec:
                if "int" in t or "float" in t:
                    encoded_xlen += 1
                else:
                    encoded_xlen += int(t[-1])
            X_scaled = np.asarray(X_scaled)[:, encoded_xlen:]
            space_scaled = np.asarray(space_scaled)[:, encoded_xlen:]

        Y = np.asarray(Y)
        evaluator = max
        if self.n_objs == 1:
            # Single objective
            if maximize:
                Y = -1.0 * Y

            if self.acq is None or n_explored < 10:
                model.fit(X_scaled, Y)
                values = model.predict(space_scaled).tolist()
                evaluator = min
            else:
                # Use the acquistion function values
                values = acquire(self.acq, X_scaled, Y, space_scaled, model,
                                 self.n_boostraps)
        else:
            # Multi-objective
            if self.acq is None or n_explored < 10:
                values = np.zeros((n_unexplored, self.n_objs))
                for i in range(self.n_objs):
                    yobj = [y[i] for y in Y]
                    model.fit(X_scaled, yobj)
                    values[:, i] = model.predict(space_scaled)
                # In exploitative strategy, randomly weight pareto optimial
                # predictions!
                values = pareto(values, maximize=maximize) * \
                         np.random.uniform(0, 1, n_unexplored)
            else:
                # Adapted from Multiobjective Optimization of Expensive Blackbox
                # Functions via Expected Maximin Improvement
                # by Joshua D. Svenson, Thomas J. Santner

                if maximize:
                    Y = -1.0 * Y

                assert (self.acq == "maximin")
                mu = np.zeros((n_unexplored, self.n_objs))
                values = np.zeros((n_unexplored, self.n_objs))
                for i in range(self.n_objs):
                    yobj = [y[i] for y in Y]
                    values[:, i], mu[:, i] = acquire("pi", X_scaled, yobj,
                                                     space_scaled, model,
                                                     self.n_boostraps,
                                                     return_means=True)
                pf = Y[pareto(Y, maximize=maximize)]
                dmaximin = np.zeros(n_unexplored)
                for i, mui in enumerate(mu):
                    mins = np.zeros(len(pf))
                    for j, pfj in enumerate(pf):
                        # select max distance to pareto point (improvements
                        # are negative) among objectives
                        mins[j] = min(mui - pfj)
                    # minimum among all pareto points of the maximum improvement
                    # among objectives. Min and max are reversed bc. minimization
                    dmaximin[i] = max(mins)

                if len(dmaximin[dmaximin < 0.0]) != 0:
                    # Predicted pareto-optimal solutions are negative so far
                    # If we are here, it means there are still predicted pareto
                    # optimal solutions. This procedure is as shown in original
                    # EI paper.
                    dmaximin = dmaximin * -1.0
                    dmaximin = dmaximin.clip(min=0)
                else:
                    # Addition if there are no predicted pareto solutions.
                    # Without this, all dmaximin values are zero if no predicted
                    # pareto solutions. With this, dmaximin values are inverted
                    # to find the 'least bad' non-pareto optimal value.
                    # Only using the 'if' block above will result in pure
                    # exploration (random) if no pareto-optimal solutions
                    # are predicted.
                    dmaximin = 1.0 / dmaximin

                pi_product = np.prod(values, axis=1)
                values = pi_product * dmaximin
            values = values.tolist()
        prediction = evaluator(values)
        index = values.index(prediction)
        return space[index]

    def _encode(self, X, dims):
        """
        Transforms data containing categorical information to "one-hot" encoded
        data, since sklearn cannot process categorical data on its own.
        
        Args:
            X ([list]): The search space, possibly containing categorical
                dimensions.
            dims: The dimensions of the search space. Used to define all
                possible choices for categorical dimensions so that categories
                are properly encoded.

        Returns:
            X ([list]): "One-hot" encoded forms of X data containing categorical
                dimensions. Search spaces which are  completely numerical are
                unchanged.
        """
        self._n_cats = 0
        self._encoding_info = []

        for i, dim in enumerate(dims):
            if type(dim[0]) in self.dtypes.others:
                cats = [0] * len(X)
                for j, x in enumerate(X):
                    cats[j] = x[i - self._n_cats]
                forward_map = {k: v for v, k in enumerate(dim)}
                inverse_map = {v: k for k, v in forward_map.items()}
                lb = LabelBinarizer()
                lb.fit([forward_map[v] for v in dim])
                binary = lb.transform([forward_map[v] for v in cats])
                for j, x in enumerate(X):
                    del (x[i - self._n_cats])
                    x += list(binary[j])
                dim_info = {'lb': lb, 'inverse_map': inverse_map,
                            'binary_len': len(binary[0])}
                self._encoding_info.append(dim_info)
                self._n_cats += 1
        return X

    def _decode(self, new_x, dims):
        """
        Convert a "one-hot" encoded point (the predicted guess) back to the
        original categorical dimensions.
        
        Args:
            new_x (list): The "one-hot" encoded new x vector predicted by the
                predictor.
            dims ([list]): The dimensions of the search space.

        Returns:
            categorical_new_x (list): The new_x vector in categorical dimensions. 

        """

        original_len = len(dims)
        static_len = original_len - self._n_cats
        categorical_new_x = []
        cat_index = 0
        tot_bin_len = 0

        for i, dim in enumerate(dims):
            if type(dim[0]) in self.dtypes.others:
                dim_info = self._encoding_info[cat_index]
                binary_len = dim_info['binary_len']
                lb = dim_info['lb']
                inverse_map = dim_info['inverse_map']
                start = static_len + tot_bin_len
                end = start + binary_len
                binary = new_x[start:end]
                int_value = lb.inverse_transform(np.asarray([binary]))[0]
                cat_value = inverse_map[int_value]
                categorical_new_x.append(cat_value)
                cat_index += 1
                tot_bin_len += binary_len
            else:
                categorical_new_x.append(new_x[i - cat_index])

        return categorical_new_x

    def _z_dims(self, XZ_unexplored, x_length):
        """
        Prepare dims to use in preprocessing for categorical dimensions.
        Gathers a list of possible dimensions from stored and current z vectors.
        Not actually used for creating a list of possible search points, only
        for helping to convert possible search points from categorical to
        integer/float.
        
        Returns:
            ([tuple]) dimensions for the z space
        """

        Z_unexplored = [z[x_length:] for z in XZ_unexplored]
        Z_explored = [doc['z'] for doc in self.c.find(self._completed,
                                                      batch_size=10000)]
        Z = Z_explored + Z_unexplored

        if not Z:
            return []

        dims = [(z, z) for z in Z[0]]

        for i, dim in enumerate(dims):
            cat_values = []
            for z in Z:
                if type(z[i]) in self.dtypes.others:
                    # the dimension is categorical
                    if z[i] not in cat_values:
                        cat_values.append(z[i])
                        dims[i] = cat_values
        return dims

    def _tolerance_check(self, x_new, X_explored, tolerances):
        """
        Duplicate checks with tolerances.

        Args:
            x_new: the new guess to be duplicate checked
            X_explored: the list of all explored guesses
            tolerances: the tolerances of each dimension

        Returns:
            True if x_new is a duplicate of a guess in X_explored.
            False if x_new is unique in the space and has yet to be tried.

        """

        if len(tolerances) != len(x_new):
            raise DimensionMismatchError("Make sure each dimension has a "
                                         "corresponding tolerance value of the "
                                         "same type! Your dimensions and the "
                                         "tolerances must be the same length "
                                         "and types. Use 'None' for categorical"
                                         " dimensions.")

        # todo: there is a more efficient way to do this: abort check for a
        # todo: pair of points as soon as one dim...
        # todo: ...is outside of tolerance

        categorical_dimensions = []
        for i in range(len(x_new)):
            if type(x_new[i]) not in self.dtypes.numbers:
                categorical_dimensions.append(i)

        for x_ex in X_explored:
            numerical_dimensions_inside_tolerance = []
            categorical_dimensions_equal = []
            for i, _ in enumerate(x_new):
                if i in categorical_dimensions:
                    if str(x_new[i]) == str(x_ex[i]):
                        categorical_dimensions_equal.append(True)
                    else:
                        categorical_dimensions_equal.append(False)
                else:
                    if abs(float(x_new[i]) - float(x_ex[i])) \
                            <= float(tolerances[i]):
                        numerical_dimensions_inside_tolerance.append(True)
                    else:
                        numerical_dimensions_inside_tolerance.append(False)

            if all(numerical_dimensions_inside_tolerance) and \
                    all(categorical_dimensions_equal):
                return True

        # If none of the points inside X_explored are close to x_new
        # (inside tolerance) in ALL dimensions, it is not a duplicate
        return False

    def _convert_native(self, a):
        """
        Convert iterables of non-native types to native types for bson storage
        in the database. For situations where .tolist() does not work.

        Args:
            a (iterable or scalar): Input list of strings, ints, or
                floats, as either numpy or native types (or others), which
                will be force-coerced to native types. Also works with scalar
                entries such as floats, ints, etc.

        Returns:
            native (list): A list of the data in a, converted to native types.

        """
        if isinstance(a, Iterable):
            try:
                # numpy conversion
                native = a.tolist()
            except AttributeError:
                native = [None] * len(a)
                for i, val in enumerate(a):
                    try:
                        native[i] = val.item()
                    except AttributeError:
                        native[i] = convert_value_to_native(val, self.dtypes)
        else:
            native = convert_value_to_native(a, self.dtypes)
        return native


class ExhaustedSpaceError(Exception):
    pass


class DimensionMismatchError(Exception):
    pass


class BatchNotReadyError(Exception):
    pass
