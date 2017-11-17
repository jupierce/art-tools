import os
import click
import tempfile
import shutil
import atexit
import yaml
import datetime

from common import assert_dir, Dir
from image import ImageMetadata
from rpm import RPMMetadata
from model import Model, Missing
from multiprocessing import Lock

DEFAULT_REGISTRIES = [
    "registry.reg-aws.openshift.com:443"
]


# Registered atexit to close out debug/record logs
def close_file(f):
    f.close()


def remove_tmp_working_dir(runtime):
    if runtime.remove_tmp_working_dir:
        shutil.rmtree(runtime.working_dir)
    else:
        click.echo("Temporary working directory preserved by operation: %s" % runtime.working_dir)


class Runtime(object):
    # Use any time it is necessary to synchronize feedback from multiple threads.
    mutex = Lock()

    # Serialize access to the debug_log, console, and record log
    log_lock = Lock()

    def __init__(self, **kwargs):
        for key, val in kwargs.items():
            self.__dict__[key] = val

        self.remove_tmp_working_dir = False
        self.group_config = None

        self.distgits_dir = None

        self.record_log = None
        self.record_log_path = None

        self.debug_log = None
        self.debug_log_path = None

        self.brew_logs_dir = None

        self.flags_dir = None

        # Registries to push to if not specified on the command line; populated by group.yml
        self.default_registries = DEFAULT_REGISTRIES

        # Map of dist-git repo name -> ImageMetadata object. Populated when group is set.
        self.image_map = {}

        # Map of dist-git repo name -> RPMMetadata object. Populated when group is set.
        self.rpm_map = {}

        # Map of source code repo aliases (e.g. "ose") to a path on the filesystem where it has been cloned.
        # See registry_repo.
        self.source_alias = {}

        # Map of stream alias to image name.
        self.stream_alias_overrides = {}

        self.initialized = False

        # Will be loaded with the streams.yml Model
        self.streams = {}

        # Create a "uuid" which will be used in FROM fields during updates
        self.uuid = datetime.datetime.now().strftime("%Y%m%d.%H%M%S")

    def initialize(self):

        if self.initialized:
            return

        # We could mark these as required and the click library would do this for us,
        # but this seems to prevent getting help from the various commands (unless you
        # specify the required parameters). This can probably be solved more cleanly, but TODO
        if self.group is None:
            click.echo("Group must be specified")
            exit(1)

        assert_dir(self.metadata_dir, "Invalid metadata-dir directory")

        if self.working_dir is None:
            self.working_dir = tempfile.mkdtemp(".tmp", "oit-")
            # This can be set to False by operations which want the working directory to be left around
            self.remove_tmp_working_dir = True
            atexit.register(remove_tmp_working_dir, self)
        else:
            assert_dir(self.working_dir, "Invalid working directory")

        self.distgits_dir = os.path.join(self.working_dir, "distgits")
        if not os.path.isdir(self.distgits_dir):
            os.mkdir(self.distgits_dir)

        self.distgits_diff_dir = os.path.join(self.working_dir, "distgits-diffs")
        if not os.path.isdir(self.distgits_diff_dir):
            os.mkdir(self.distgits_diff_dir)

        self.debug_log_path = os.path.join(self.working_dir, "debug.log")
        self.debug_log = open(self.debug_log_path, 'a')
        atexit.register(close_file, self.debug_log)

        self.record_log_path = os.path.join(self.working_dir, "record.log")
        self.record_log = open(self.record_log_path, 'a')
        atexit.register(close_file, self.record_log)

        # Directory where brew-logs will be downloaded after a build
        self.brew_logs_dir = os.path.join(self.working_dir, "brew-logs")
        if not os.path.isdir(self.brew_logs_dir):
            os.mkdir(self.brew_logs_dir)

        # Directory for flags between invocations in the same working-dir
        self.flags_dir = os.path.join(self.working_dir, "flags")
        if not os.path.isdir(self.flags_dir):
            os.mkdir(self.flags_dir)

        group_dir = os.path.join(self.metadata_dir, "groups", self.group)
        assert_dir(group_dir, "Cannot find group directory")

        images_dir = os.path.join(group_dir, 'images')
        assert_dir(group_dir, "Cannot find images directory for {}".format(group_dir))

        rpms_dir = os.path.join(group_dir, 'rpms')
        assert_dir(group_dir, "Cannot find rpms directory for {}".format(group_dir))

        self.info("Searching group directory: %s" % group_dir)
        with Dir(group_dir):
            with open("group.yml", "r") as f:
                group_yml = f.read()

            self.group_config = Model(yaml.load(group_yml))

            if self.group_config.name != self.group:
                raise IOError(
                    "Name in group.yml does not match group name. Someone may have copied this group without updating group.yml (make sure to check branch)")

            if self.group_config.excludes is not Missing and self.exclude is None:
                self.exclude = self.group_config.excludes

            if self.group_config.includes is not Missing and self.include is None:
                self.include = self.group_config.includes

            if self.branch is None:
                if self.group_config.branch is not Missing:
                    self.branch = self.group_config.branch
                    self.info("Using branch from group.yml: %s" % self.branch)
                else:
                    self.info("No branch specified either in group.yml or on the command line; all included images will need to specify their own.")
            else:
                self.info("Using branch from command line: %s" % self.branch)

            if len(self.include) > 0:
                self.info("Include list set to: %s" % str(self.include))

            if len(self.exclude) > 0:
                self.info("Exclude list set to: %s" % str(self.exclude))

            with Dir(images_dir):
                images_list = [x for x in os.listdir(".") if os.path.isdir(x)]

            with Dir(rpms_dir):
                rpms_list = [x for x in os.listdir(".") if os.path.isdir(x)]

            # for later checking we need to remove from the lists, but they are tuples. Clone to list
            image_include = []
            image_optional_include = []
            image_exclude = []
            for name in images_list:
                if name in self.include:
                    image_include.append(name)
                if name in self.optional_include:
                    image_optional_include.append(name)
                if name in self.exclude:
                    image_exclude.append(name)

            rpm_include = []
            rpm_optional_include = []
            rpm_exclude = []
            for name in rpms_list:
                if name in self.include:
                    rpm_include.append(name)
                if name in self.optional_include:
                    rpm_optional_include.append(name)
                if name in self.exclude:
                    rpm_exclude.append(name)

            missed_include = set(self.include) - set(image_include + rpm_include)
            if len(missed_include) > 0:
                raise IOError('Unable to find the following images or rpms configs: {}'.format(', '.join(missed_include)))

            missed_optional_include = set(self.optional_include) - set(image_optional_include + rpm_optional_include)
            if len(missed_optional_include) > 0:
                self.info('The following images or rpms were not found, but optional: {}'.format(', '.join(missed_optional_include)))

            def gen_ImageMetadata(name):
                self.image_map[name] = ImageMetadata(self, name, name)

            def gen_RPMMetadata(name):
                self.rpm_map[name] = RPMMetadata(self, name, name)

            def collect_configs(search_type, search_dir, name_list, include, optional_include, exclude, gen):
                check_include = len(include) > 0
                check_optional_include = len(optional_include) > 0
                check_exclude = len(exclude) > 0

                with Dir(search_dir):
                    for distgit_repo_name in name_list:
                        if check_include or check_optional_include:
                            if check_include and distgit_repo_name in include:
                                self.info("include: " + distgit_repo_name)
                                include.remove(distgit_repo_name)
                            elif check_optional_include and distgit_repo_name in optional_include:
                                self.info("optional_include: " + distgit_repo_name)
                                optional_include.remove(distgit_repo_name)
                            else:
                                self.info("{} skip: {}".format(search_type, distgit_repo_name))
                                self.log_verbose("Skipping {} {} since it is not in the include list".format(search_type, distgit_repo_name))
                                continue

                        if check_exclude and distgit_repo_name in self.exclude:
                            self.info("{} exclude: {}".format(search_type, distgit_repo_name))
                            self.log_verbose("Skipping {} {} since it is in the exclude list".format(search_type, distgit_repo_name))
                            continue

                        gen(distgit_repo_name)

            collect_configs('image', images_dir, images_list,
                            image_include, image_optional_include,
                            image_exclude, gen_ImageMetadata)

            collect_configs('rpm', rpms_dir, rpms_list,
                            rpm_include, rpm_optional_include,
                            rpm_exclude, gen_RPMMetadata)

        if len(self.image_map) + len(self.rpm_map) == 0:
            raise IOError("No image or rpm metadata directories found within: {}".format(group_dir))

        # Read in the streams definite for this group if one exists
        streams_path = os.path.join(group_dir, "streams.yml")
        if os.path.isfile(streams_path):
            with open(streams_path, "r") as s:
                self.streams = Model(yaml.load(s.read()))

    def log_verbose(self, message):
        with self.log_lock:
            if self.verbose:
                click.echo(message)
            self.debug_log.write(message + "\n")
            self.debug_log.flush()

    def info(self, message, debug=None):
        if self.verbose:
            if debug is not None:
                self.log_verbose("%s [%s]" % (message, debug))
            else:
                self.log_verbose(message)
        else:
            with self.log_lock:
                click.echo(message)

    def images(self):
        return self.image_map.values()

    def register_source_alias(self, alias, path):
        self.info("Registering source alias %s: %s" % (alias, path))
        path = os.path.abspath(path)
        assert_dir(path, "Error registering source alias %s" % alias)
        self.source_alias[alias] = path

    def register_stream_alias(self, alias, image):
        self.info("Registering image stream alias override %s: %s" % (alias, image))
        self.stream_alias_overrides[alias] = image

    def add_record(self, record_type, **kwargs):
        """
        Records an action taken by oit that needs to be communicated to outside systems. For example,
        the update a Dockerfile which needs to be reviewed by an owner. Each record is encoded on a single
        line in the record.log. Records cannot contain line feeds -- if you need to communicate multi-line
        data, create a record with a path to a file in the working directory.
        :param record_type: The type of record to create.
        :param kwargs: key/value pairs

        A record line is designed to be easily parsed and formatted as:
        record_type|key1=value1|key2=value2|...|
        """

        # Multiple image build processes could be calling us with action simultaneously, so
        # synchronize output to the file.
        with self.log_lock:
            record = "%s|" % record_type
            for k, v in kwargs.iteritems():
                assert ("\n" not in str(k))
                # Make sure the values have no linefeeds as this would interfere with simple parsing.
                v = str(v).replace("\n", " ;;; ").replace("\r", "")
                record += "%s=%s|" % (k, v)

            # Add the record to the file
            self.record_log.write("%s\n" % record)
            self.record_log.flush()

    def add_distgits_diff(self, distgit, diff):
        """
        Records the diff of changes applied to a distgit repo.
        """

        with open(os.path.join(self.distgits_diff_dir, distgit + '.patch'), 'w') as f:
            f.write(diff)

    def resolve_image(self, distgit_name, required=True):
        if distgit_name not in self.image_map:
            if not required:
                return None
            raise IOError("Unable to find image metadata in group / included images: %s" % distgit_name)
        return self.image_map[distgit_name]

    def resolve_stream(self, stream_name):

        # If the stream has an override from the command line, return it.
        if stream_name in self.stream_alias_overrides:
            return self.stream_alias_overrides[stream_name]

        if stream_name not in self.streams:
            raise IOError("Unable to find definition for stream: %s" % stream_name)

        return self.streams[stream_name]

    def _flag_file(self,flag_name):
        return os.path.join(self.flags_dir, flag_name)

    def flag_create(self, flag_name, msg=""):
        with open(self._flag_file(flag_name), 'w') as f:
            f.write(msg)

    def flag_exists(self, flag_name):
        return os.path.isfile(self._flag_file(flag_name))

    def flag_remove(self, flag_name):
        if self.flag_exists(flag_name):
            os.remove(self._flag_file(flag_name))
