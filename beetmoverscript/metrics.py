import logging

log = logging.getLogger(__name__)


class LocalClient(object):
    def __init__(self, *args, **kwargs):
        pass

    def metric(self, measurement_name, value, tags):
        log.info("{}, {}, {}".format(measurement_name, value, tags))


try:
    from telegraf.client import TelegrafClient
    metrics_client = TelegrafClient()
except (ImportError, ModuleNotFoundError):
    log.warning("Telegraf client not available, metrics collection disabled.")
    metrics_client = LocalClient()
