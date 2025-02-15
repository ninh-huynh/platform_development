#!/usr/bin/env python3

import os
import re
import shutil
import subprocess
import sys
import tempfile
import collections


SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))

try:
    AOSP_DIR = os.environ['ANDROID_BUILD_TOP']
except KeyError:
    print('error: ANDROID_BUILD_TOP environment variable is not set.',
          file=sys.stderr)
    sys.exit(1)

BUILTIN_HEADERS_DIR = (
    os.path.join(AOSP_DIR, 'bionic', 'libc', 'include'),
    os.path.join(AOSP_DIR, 'external', 'libcxx', 'include'),
    os.path.join(AOSP_DIR, 'prebuilts', 'clang-tools', 'linux-x86',
                 'clang-headers'),
)

SO_EXT = '.so'
SOURCE_ABI_DUMP_EXT_END = '.lsdump'
SOURCE_ABI_DUMP_EXT = SO_EXT + SOURCE_ABI_DUMP_EXT_END
VENDOR_SUFFIX = '.vendor'

DEFAULT_CPPFLAGS = ['-x', 'c++', '-std=c++11']
DEFAULT_CFLAGS = ['-std=gnu99']
DEFAULT_HEADER_FLAGS = ["-dump-function-declarations"]
DEFAULT_FORMAT = 'ProtobufTextFormat'


class Target(object):
    def __init__(self, is_2nd, product):
        extra = '_2ND' if is_2nd else ''
        build_vars_to_fetch = ['TARGET_ARCH',
                               'TARGET{}_ARCH'.format(extra),
                               'TARGET{}_ARCH_VARIANT'.format(extra),
                               'TARGET{}_CPU_VARIANT'.format(extra)]
        build_vars = get_build_vars_for_product(build_vars_to_fetch, product)
        self.primary_arch = build_vars[0]
        assert self.primary_arch != ''
        self.arch = build_vars[1]
        self.arch_variant = build_vars[2]
        self.cpu_variant = build_vars[3]

    def get_arch_str(self):
        """Return a string that represents the architecture and the primary
        architecture.
        """
        if not self.arch or self.arch == self.primary_arch:
            return self.primary_arch
        return self.arch + '_' + self.primary_arch

    def get_arch_cpu_str(self):
        """Return a string that represents the architecture, the architecture
        variant, and the CPU variant.

        If TARGET_ARCH == TARGET_ARCH_VARIANT, soong makes targetArchVariant
        empty. This is the case for aosp_x86_64.
        """
        if not self.arch_variant or self.arch_variant == self.arch:
            arch_variant = ''
        else:
            arch_variant = '_' + self.arch_variant

        if not self.cpu_variant or self.cpu_variant == 'generic':
            cpu_variant = ''
        else:
            cpu_variant = '_' + self.cpu_variant

        return self.arch + arch_variant + cpu_variant


def _validate_dump_content(dump_path):
    """Make sure that the dump contains relative source paths."""
    with open(dump_path, 'r') as f:
        for line_number, line in enumerate(f, 1):
            start = 0
            while True:
                start = line.find(AOSP_DIR, start)
                if start < 0:
                    break
                # The substring is not preceded by a common path character.
                if start == 0 or not (line[start - 1].isalnum() or
                                      line[start - 1] in '.-_/'):
                    raise ValueError(f'{dump_path} contains absolute path to '
                                     f'$ANDROID_BUILD_TOP at line '
                                     f'{line_number}:\n{line}')
                start += len(AOSP_DIR)


def copy_reference_dump(lib_path, reference_dump_dir):
    reference_dump_path = os.path.join(
        reference_dump_dir, os.path.basename(lib_path))
    os.makedirs(os.path.dirname(reference_dump_path), exist_ok=True)
    _validate_dump_content(lib_path)
    shutil.copyfile(lib_path, reference_dump_path)
    print('Created abi dump at', reference_dump_path)
    return reference_dump_path


def run_header_abi_dumper(input_path, output_path, cflags=tuple(),
                          export_include_dirs=tuple(), flags=tuple()):
    """Run header-abi-dumper to dump ABI from `input_path` and the output is
    written to `output_path`."""
    input_ext = os.path.splitext(input_path)[1]
    cmd = ['header-abi-dumper', '-o', output_path, input_path]
    for dir in export_include_dirs:
        cmd += ['-I', dir]
    cmd += flags
    if '-output-format' not in flags:
        cmd += ['-output-format', DEFAULT_FORMAT]
    if input_ext == ".h":
        cmd += DEFAULT_HEADER_FLAGS
    cmd += ['--']
    cmd += cflags
    if input_ext in ('.cpp', '.cc', '.h'):
        cmd += DEFAULT_CPPFLAGS
    else:
        cmd += DEFAULT_CFLAGS

    for dir in BUILTIN_HEADERS_DIR:
        cmd += ['-isystem', dir]
    # The export include dirs imply local include dirs.
    for dir in export_include_dirs:
        cmd += ['-I', dir]
    subprocess.check_call(cmd, cwd=AOSP_DIR)
    _validate_dump_content(output_path)


def run_header_abi_linker(inputs, output_path, version_script, api, arch,
                          flags=tuple()):
    """Link inputs, taking version_script into account"""
    cmd = ['header-abi-linker', '-o', output_path, '-v', version_script,
           '-api', api, '-arch', arch]
    cmd += flags
    if '-input-format' not in flags:
        cmd += ['-input-format', DEFAULT_FORMAT]
    if '-output-format' not in flags:
        cmd += ['-output-format', DEFAULT_FORMAT]
    cmd += inputs
    subprocess.check_call(cmd, cwd=AOSP_DIR)
    _validate_dump_content(output_path)


def make_targets(product, variant, targets):
    make_cmd = ['build/soong/soong_ui.bash', '--make-mode', '-j',
                'TARGET_PRODUCT=' + product, 'TARGET_BUILD_VARIANT=' + variant]
    make_cmd += targets
    subprocess.check_call(make_cmd, cwd=AOSP_DIR)


def make_tree(product, variant):
    """Build all lsdump files."""
    return make_targets(product, variant, ['findlsdumps'])


def make_libraries(product, variant, vndk_version, targets, libs,
                   exclude_tags):
    """Build lsdump files for specific libs."""
    lsdump_paths = read_lsdump_paths(product, variant, vndk_version, targets,
                                     exclude_tags, build=True)
    make_target_paths = []
    for name in libs:
        if not (name in lsdump_paths and lsdump_paths[name]):
            raise KeyError('Cannot find lsdump for %s.' % name)
        for tag_path_dict in lsdump_paths[name].values():
            make_target_paths.extend(tag_path_dict.values())
    make_targets(product, variant, make_target_paths)


def get_lsdump_paths_file_path(product, variant):
    """Get the path to lsdump_paths.txt."""
    product_out = get_build_vars_for_product(
        ['PRODUCT_OUT'], product, variant)[0]
    return os.path.join(product_out, 'lsdump_paths.txt')


def _get_module_variant_sort_key(suffix):
    for variant in suffix.split('_'):
        match = re.match(r'apex(\d+)$', variant)
        if match:
            return (int(match.group(1)), suffix)
    return (-1, suffix)


def _get_module_variant_dir_name(tag, vndk_version, arch_cpu_str):
    """Return the module variant directory name.

    For example, android_x86_shared, android_vendor.R_arm_armv7-a-neon_shared.
    """
    if tag in ('LLNDK', 'NDK', 'PLATFORM'):
        return f'android_{arch_cpu_str}_shared'
    if tag.startswith('VNDK') or tag == 'VENDOR':
        return f'android_vendor.{vndk_version}_{arch_cpu_str}_shared'
    if tag == 'PRODUCT':
        return f'android_product.{vndk_version}_{arch_cpu_str}_shared'
    raise ValueError(tag + ' is not a known tag.')


def _read_lsdump_paths(lsdump_paths_file_path, vndk_version, targets,
                       exclude_tags):
    """Read lsdump paths from lsdump_paths.txt for each libname and variant.

    This function returns a dictionary, {lib_name: {arch_cpu: {tag: path}}}.
    For example,
    {
      "libc": {
        "x86_x86_64": {
          "NDK": "path/to/libc.so.lsdump"
        }
      }
    }
    """
    lsdump_paths = collections.defaultdict(
        lambda: collections.defaultdict(dict))
    suffixes = collections.defaultdict(dict)

    with open(lsdump_paths_file_path, 'r') as lsdump_paths_file:
        for line in lsdump_paths_file:
            tag, path = (x.strip() for x in line.split(':', 1))
            if not path or tag in exclude_tags:
                continue
            dir_path, filename = os.path.split(path)
            if not filename.endswith(SOURCE_ABI_DUMP_EXT):
                continue
            libname = filename[:-len(SOURCE_ABI_DUMP_EXT)]
            if not libname:
                continue
            # dir_path may contain soong config hash.
            # For example, the following dir_paths are valid.
            # android_x86_x86_64_shared/012abc/libc.so.lsdump
            # android_x86_x86_64_shared/libc.so.lsdump
            dirnames = []
            dir_path, dirname = os.path.split(dir_path)
            dirnames.append(dirname)
            dirname = os.path.basename(dir_path)
            dirnames.append(dirname)
            for target in targets:
                arch_cpu = target.get_arch_cpu_str()
                prefix = _get_module_variant_dir_name(tag, vndk_version,
                                                      arch_cpu)
                variant = next((d for d in dirnames if d.startswith(prefix)),
                               None)
                if not variant:
                    continue
                new_suffix = variant[len(prefix):]
                old_suffix = suffixes[libname].get(arch_cpu)
                if (not old_suffix or
                        _get_module_variant_sort_key(new_suffix) >
                        _get_module_variant_sort_key(old_suffix)):
                    lsdump_paths[libname][arch_cpu][tag] = path
                    suffixes[libname][arch_cpu] = new_suffix
    return lsdump_paths


def read_lsdump_paths(product, variant, vndk_version, targets, exclude_tags,
                      build):
    """Build lsdump_paths.txt and read the paths."""
    lsdump_paths_file_path = get_lsdump_paths_file_path(product, variant)
    lsdump_paths_file_abspath = os.path.join(AOSP_DIR, lsdump_paths_file_path)
    if build:
        if os.path.lexists(lsdump_paths_file_abspath):
            os.unlink(lsdump_paths_file_abspath)
        make_targets(product, variant, [lsdump_paths_file_path])
    return _read_lsdump_paths(lsdump_paths_file_abspath, vndk_version,
                              targets, exclude_tags)


def find_lib_lsdumps(lsdump_paths, libs, target):
    """Find the lsdump corresponding to libs for the given target.

    This function returns a list of (tag, absolute_path).
    For example,
    [
      (
        "NDK",
        "/path/to/libc.so.lsdump"
      )
    ]
    """
    arch_cpu = target.get_arch_cpu_str()
    result = []
    if libs:
        for lib_name in libs:
            if not (lib_name in lsdump_paths and
                    arch_cpu in lsdump_paths[lib_name]):
                raise KeyError('Cannot find lsdump for %s, %s.' %
                               (lib_name, arch_cpu))
            result.extend(lsdump_paths[lib_name][arch_cpu].items())
    else:
        for arch_tag_path_dict in lsdump_paths.values():
            result.extend(arch_tag_path_dict[arch_cpu].items())
    return [(tag, os.path.join(AOSP_DIR, path)) for tag, path in result]


def run_abi_diff(old_dump_path, new_dump_path, output_path, arch, lib_name,
                 flags):
    abi_diff_cmd = ['header-abi-diff', '-new', new_dump_path, '-old',
                    old_dump_path, '-arch', arch, '-lib', lib_name,
                    '-o', output_path]
    abi_diff_cmd += flags
    if '-input-format-old' not in flags:
        abi_diff_cmd += ['-input-format-old', DEFAULT_FORMAT]
    if '-input-format-new' not in flags:
        abi_diff_cmd += ['-input-format-new', DEFAULT_FORMAT]
    return subprocess.run(abi_diff_cmd).returncode


def run_and_read_abi_diff(old_dump_path, new_dump_path, arch, lib_name,
                          flags=tuple()):
    with tempfile.TemporaryDirectory() as tmp:
        output_name = os.path.join(tmp, lib_name) + '.abidiff'
        result = run_abi_diff(old_dump_path, new_dump_path, output_name, arch,
                              lib_name, flags)
        with open(output_name, 'r') as output_file:
            return result, output_file.read()


def get_build_vars_for_product(names, product=None, variant=None):
    """ Get build system variable for the launched target."""

    if product is None and 'ANDROID_PRODUCT_OUT' not in os.environ:
        return None

    env = os.environ.copy()
    if product:
        env['TARGET_PRODUCT'] = product
    if variant:
        env['TARGET_BUILD_VARIANT'] = variant
    cmd = [
        os.path.join('build', 'soong', 'soong_ui.bash'),
        '--dumpvars-mode', '-vars', ' '.join(names),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=AOSP_DIR, env=env)
    out, err = proc.communicate()

    if proc.returncode != 0:
        print("error: %s" % err.decode('utf-8'), file=sys.stderr)
        return None

    build_vars = out.decode('utf-8').strip().splitlines()

    build_vars_list = []
    for build_var in build_vars:
        value = build_var.partition('=')[2]
        build_vars_list.append(value.replace('\'', ''))
    return build_vars_list
