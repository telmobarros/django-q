"""Provides task functionality."""
from multiprocessing import Queue, Value

# django
from django.utils import timezone

# local
import time
import signing
import cluster
from django_q.conf import Conf, logger
from django_q.models import Schedule, Task
from django_q.humanhash import uuid
from django_q.brokers import get_broker


def async(func, *args, **kwargs):
    """Queue a task for the cluster."""
    # get options from q_options dict or direct from kwargs
    options = kwargs.pop('q_options', kwargs)
    broker = options.pop('broker', get_broker())
    # pop optionals
    opts = {'hook': None,
            'group': None,
            'save': None,
            'sync': None,
            'cached': Conf.CACHED,
            'iter_count': None,
            'iter_cached': None,
            'chain': None}
    for key in opts:
        opts[key] = options.pop(key, opts[key])
        # get an id
    tag = uuid()
    # build the task package
    task = {'id': tag[1], 'name': tag[0],
            'func': func,
            'args': args,
            'kwargs': kwargs,
            'started': timezone.now()}
    # push optionals
    for key in opts:
        if opts[key] is not None:
            task[key] = opts[key]
    # sign it
    pack = signing.SignedPackage.dumps(task)
    if task.get('sync', False) or Conf.SYNC:
        return _sync(pack)
    # push it
    broker.enqueue(pack)
    logger.debug('Pushed {}'.format(tag))
    return task['id']


def schedule(func, *args, **kwargs):
    """
    Create a schedule.

    :param func: function to schedule.
    :param args: function arguments.
    :param name: optional name for the schedule.
    :param hook: optional result hook function.
    :type schedule_type: Schedule.TYPE
    :param repeats: how many times to repeat. 0=never, -1=always.
    :param next_run: Next scheduled run.
    :type next_run: datetime.datetime
    :param kwargs: function keyword arguments.
    :return: the schedule object.
    :rtype: Schedule
    """
    name = kwargs.pop('name', None)
    hook = kwargs.pop('hook', None)
    schedule_type = kwargs.pop('schedule_type', Schedule.ONCE)
    minutes = kwargs.pop('minutes', None)
    repeats = kwargs.pop('repeats', -1)
    next_run = kwargs.pop('next_run', timezone.now())

    return Schedule.objects.create(name=name,
                                   func=func,
                                   hook=hook,
                                   args=args,
                                   kwargs=kwargs,
                                   schedule_type=schedule_type,
                                   minutes=minutes,
                                   repeats=repeats,
                                   next_run=next_run
                                   )


def result(task_id, wait=0, cached=Conf.CACHED):
    """
    Return the result of the named task.

    :type task_id: str or uuid
    :param task_id: the task name or uuid
    :type wait: int
    :param wait: number of milliseconds to wait for a result
    :param bool cached: run this against the cache backend
    :return: the result object of this task
    :rtype: object
    """
    if cached:
        return result_cached(task_id, wait)
    start = time.time()
    while True:
        r = Task.get_result(task_id)
        if r:
            return r
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def result_cached(task_id, wait=0, broker=None):
    """
     Return the result from the cache backend
    """
    if not broker:
        broker = get_broker()
    start = time.time()
    while True:
        r = broker.cache.get('{}:{}'.format(broker.list_key, task_id))
        if r:
            return signing.SignedPackage.loads(r)['result']
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def result_group(group_id, failures=False, wait=0, count=None, cached=Conf.CACHED):
    """
    Return a list of results for a task group.

    :param str group_id: the group id
    :param bool failures: set to True to include failures
    :param int count: Block until there are this many results in the group
    :param bool cached: run this against the cache backend
    :return: list or results
    """
    if cached:
        return result_group_cached(group_id, failures, wait, count)
    start = time.time()
    if count:
        while True:
            if count_group(group_id) == count or wait and (time.time() - start) * 1000 >= wait >= 0:
                break
            time.sleep(0.01)
    while True:
        r = Task.get_result_group(group_id, failures)
        if r:
            return r
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def result_group_cached(group_id, failures=False, wait=0, count=None, broker=None):
    """
    Return a list of results for a task group from the cache backend
    """
    if not broker:
        broker = get_broker()
    start = time.time()
    if count:
        while True:
            if count_group_cached(group_id) == count or wait and (time.time() - start) * 1000 >= wait > 0:
                break
            time.sleep(0.01)
    while True:
        group_list = broker.cache.get('{}:{}:keys'.format(broker.list_key, group_id))
        if group_list:
            result_list = []
            for task_key in group_list:
                task = signing.SignedPackage.loads(broker.cache.get(task_key))
                if task['success'] or failures:
                    result_list.append(task['result'])
            return result_list
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def fetch(task_id, wait=0, cached=Conf.CACHED):
    """
    Return the processed task.

    :param task_id: the task name or uuid
    :type task_id: str or uuid
    :param wait: the number of milliseconds to wait for a result
    :type wait: int
    :param bool cached: run this against the cache backend
    :return: the full task object
    :rtype: Task
    """
    if cached:
        return fetch_cached(task_id, wait)
    start = time.time()
    while True:
        t = Task.get_task(task_id)
        if t:
            return t
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def fetch_cached(task_id, wait=0, broker=None):
    """
    Return the processed task from the cache backend
    """
    if not broker:
        broker = get_broker()
    start = time.time()
    while True:
        r = broker.cache.get('{}:{}'.format(broker.list_key, task_id))
        if r:
            task = signing.SignedPackage.loads(r)
            t = Task(id=task['id'],
                     name=task['name'],
                     func=task['func'],
                     hook=task.get('hook'),
                     args=task['args'],
                     kwargs=task['kwargs'],
                     started=task['started'],
                     stopped=task['stopped'],
                     result=task['result'],
                     success=task['success'])
            return t
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def fetch_group(group_id, failures=True, wait=0, count=None, cached=Conf.CACHED):
    """
    Return a list of Tasks for a task group.

    :param str group_id: the group id
    :param bool failures: set to False to exclude failures
    :param bool cached: run this against the cache backend
    :return: list of Tasks
    """
    if cached:
        return fetch_group_cached(group_id, failures, wait, count)
    start = time.time()
    if count:
        while True:
            if count_group(group_id) == count or wait and (time.time() - start) * 1000 >= wait >= 0:
                break
            time.sleep(0.01)
    while True:
        r = Task.get_task_group(group_id, failures)
        if r:
            return r
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def fetch_group_cached(group_id, failures=True, wait=0, count=None, broker=None):
    """
    Return a list of Tasks for a task group in the cache backend
    """
    if not broker:
        broker = get_broker()
    start = time.time()
    if count:
        while True:
            if count_group_cached(group_id) == count or wait and (time.time() - start) * 1000 >= wait >= 0:
                break
            time.sleep(0.01)
    while True:
        group_list = broker.cache.get('{}:{}:keys'.format(broker.list_key, group_id))
        if group_list:
            task_list = []
            for task_key in group_list:
                task = signing.SignedPackage.loads(broker.cache.get(task_key))
                if task['success'] or failures:
                    t = Task(id=task['id'],
                             name=task['name'],
                             func=task['func'],
                             hook=task.get('hook'),
                             args=task['args'],
                             kwargs=task['kwargs'],
                             started=task['started'],
                             stopped=task['stopped'],
                             result=task['result'],
                             group=task.get('group'),
                             success=task['success'])
                    task_list.append(t)
            return task_list
        if (time.time() - start) * 1000 >= wait >= 0:
            break
        time.sleep(0.01)


def count_group(group_id, failures=False, cached=Conf.CACHED):
    """
    Count the results in a group.

    :param str group_id: the group id
    :param bool failures: Returns failure count if True
    :param bool cached: run this against the cache backend
    :return: the number of tasks/results in a group
    :rtype: int
    """
    if cached:
        return count_group_cached(group_id, failures)
    return Task.get_group_count(group_id, failures)


def count_group_cached(group_id, failures=False, broker=None):
    """
    Count the results in a group in the cache backend
    """
    if not broker:
        broker = get_broker()
    group_list = broker.cache.get('{}:{}:keys'.format(broker.list_key, group_id))
    if group_list:
        if not failures:
            return len(group_list)
        failure_count = 0
        for task_key in group_list:
            task = signing.SignedPackage.loads(broker.cache.get(task_key))
            if not task['success']:
                failure_count += 1
        return failure_count


def delete_group(group_id, tasks=False, cached=Conf.CACHED):
    """
    Delete a group.

    :param str group_id: the group id
    :param bool tasks: If set to True this will also delete the group tasks.
    Otherwise just the group label is removed.
    :param bool cached: run this against the cache backend
    :return:
    """
    if cached:
        return delete_group_cached(group_id)
    return Task.delete_group(group_id, tasks)


def delete_group_cached(group_id, broker=None):
    """
    Delete a group from the cache backend
    """
    if not broker:
        broker = get_broker()
    group_key = '{}:{}:keys'.format(broker.list_key, group_id)
    group_list = broker.cache.get(group_key)
    broker.cache.delete_many(group_list)
    broker.cache.delete(group_key)


def delete_cached(task_id, broker=None):
    """
    Delete a task from the cache backend
    """
    if not broker:
        broker = get_broker()
    return broker.cache.delete('{}:{}'.format(broker.list_key, task_id))


def queue_size(broker=None):
    """
    Returns the current queue size.
    Note that this doesn't count any tasks currently being processed by workers.

    :param broker: optional broker
    :return: current queue size
    :rtype: int
    """
    if not broker:
        broker = get_broker()
    return broker.queue_size()


def async_iter(func, args_iter, **kwargs):
    """
    async a function with iterable arguments
    """
    iter_count = len(args_iter)
    iter_group = uuid()[1]
    # clean up the kwargs
    options = kwargs.get('q_options', kwargs)
    options.pop('hook', None)
    options['broker'] = options.get('broker', get_broker())
    options['group'] = iter_group
    options['iter_count'] = iter_count
    if options.get('cached', None):
        options['iter_cached'] = options['cached']
    options['cached'] = True
    # save the original arguments
    broker = options['broker']
    broker.cache.set('{}:{}:args'.format(broker.list_key, iter_group), signing.SignedPackage.dumps(args_iter))
    for args in args_iter:
        if type(args) is not tuple:
            args = (args,)
        async(func, *args, **options)
    return iter_group


def async_chain(chain, group=None, cached=Conf.CACHED, sync=Conf.SYNC, broker=None):
    """
    async a chain of tasks
    the chain must be in the format [(func,(args),{kwargs}),(func,(args),{kwargs})]
    """
    if not group:
        group = uuid()[1]
    args = ()
    kwargs = {}
    task = chain.pop(0)
    if type(task) is not tuple:
        task = (task,)
    if len(task) > 1:
        args = task[1]
    if len(task) > 2:
        kwargs = task[2]
    kwargs['chain'] = chain
    kwargs['group'] = group
    kwargs['cached'] = cached
    kwargs['sync'] = sync
    kwargs['broker'] = broker or get_broker()
    async(task[0], *args, **kwargs)
    return group


class Iter(object):
    """
    An async task with iterable arguments
    """

    def __init__(self, func=None, args=None, kwargs=None, cached=Conf.CACHED, sync=Conf.SYNC, broker=None):
        self.func = func
        self.args = args or []
        self.kwargs = kwargs or {}
        self.id = ''
        self.broker = broker or get_broker()
        self.cached = cached
        self.sync = sync
        self.started = False

    def append(self, *args):
        """
        add arguments to the set
        """
        self.args.append(args)
        if self.started:
            self.started = False
        return self.length()

    def run(self):
        """
        Start queueing the tasks to the worker cluster
        :return: the task id
        """
        self.kwargs['cached'] = self.cached
        self.kwargs['sync'] = self.sync
        self.kwargs['broker'] = self.broker
        self.id = async_iter(self.func, self.args, **self.kwargs)
        self.started = True
        return self.id

    def result(self, wait=0):
        """
        return the full list of results.
        :param int wait: how many milliseconds to wait for a result
        :return: an unsorted list of results
        """
        if self.started:
            return result(self.id, wait=wait, cached=self.cached)

    def fetch(self, wait=0):
        """
        get the task result objects.
        :param int wait: how many milliseconds to wait for a result
        :return: an unsorted list of task objects
        """
        if self.started:
            return fetch(self.id, wait=wait, cached=self.cached)

    def length(self):
        """
        get the length of the arguments list
        :return int: length of the argument list
        """
        return len(self.args)


class Chain(object):
    """
    A sequential chain of tasks
    """

    def __init__(self, chain=None, group=None, cached=Conf.CACHED, sync=Conf.SYNC):
        self.chain = chain or []
        self.group = group or ''
        self.broker = get_broker()
        self.cached = cached
        self.sync = sync
        self.started = False

    def append(self, func, *args, **kwargs):
        """
        add a task to the chain
        takes the same parameters as async()
        """
        self.chain.append((func, args, kwargs))
        # remove existing results
        if self.started:
            delete_group(self.group)
            self.started = False
        return self.length()

    def run(self):
        """
        Start queueing the chain to the worker cluster
        :return: the chain's group id
        """
        self.group = async_chain(chain=self.chain[:], group=self.group, cached=self.cached, sync=self.sync,
                                 broker=self.broker)
        self.started = True
        return self.group

    def result(self, wait=0):
        """
        return the full list of results from the chain when it finishes. blocks until timeout.
        :param int wait: how many milliseconds to wait for a result
        :return: an unsorted list of results
        """
        if self.started:
            return result_group(self.group, wait=wait, count=self.length(), cached=self.cached)

    def fetch(self, failures=True, wait=0):
        """
        get the task result objects from the chain when it finishes. blocks until timeout.
        :param failures: include failed tasks
        :param int wait: how many milliseconds to wait for a result
        :return: an unsorted list of task objects
        """
        if self.started:
            return fetch_group(self.group, failures=failures, wait=wait, count=self.length(), cached=self.cached)

    def current(self):
        """
        get the index of the currently executing chain element
        :return int: current chain index
        """
        if not self.started:
            return None
        return count_group(self.group, cached=self.cached)

    def length(self):
        """
        get the length of the chain
        :return int: length of the chain
        """
        return len(self.chain)


def _sync(pack):
    """Simulate a package travelling through the cluster."""
    task_queue = Queue()
    result_queue = Queue()
    task = signing.SignedPackage.loads(pack)
    task_queue.put(task)
    task_queue.put('STOP')
    cluster.worker(task_queue, result_queue, Value('f', -1))
    result_queue.put('STOP')
    cluster.monitor(result_queue)
    return task['id']
