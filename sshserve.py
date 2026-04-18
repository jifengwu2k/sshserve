# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from __future__ import print_function

import argparse
import ctypes
import logging
import fcntl
import os
import pty
import select
import socket
import struct
import sys
import termios
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import ctypes_unicode_proclaunch as proclaunch
import paramiko
from get_unicode_shell import get_unicode_shell
from read_unicode_environment_variables_dictionary import read_unicode_environment_variables_dictionary

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 2222
DEFAULT_LISTEN_BACKLOG = 100
DEFAULT_CHANNEL_WAIT_TIMEOUT_SECONDS = 30.0
DEFAULT_SELECT_TIMEOUT_SECONDS = 1.0
SESSION_CHANNEL_KIND = 'session'
SFTP_SUBSYSTEM_NAME = 'sftp'

LIBC_SETSID = proclaunch.libc.setsid
LIBC_SETSID.restype = ctypes.c_int
LIBC_IOCTL = proclaunch.libc.ioctl
LIBC_IOCTL.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_int]
LIBC_IOCTL.restype = ctypes.c_int

logging.basicConfig(level=logging.INFO, format='%(message)s')
LOGGER = logging.getLogger(__name__)


def launch_pty_process(arguments, slave_file_descriptor):
    # type: (Sequence[str], int) -> int
    environment = read_unicode_environment_variables_dictionary()
    executable_path = next(proclaunch.find_unicode_executable(arguments[0]), None)
    if executable_path is None:
        raise ValueError('Cannot find executable %s' % arguments[0])
    argument_array = proclaunch.utf_8_c_char_p_array_from_unicode_strings([executable_path] + list(arguments[1:]))
    environment_array = proclaunch.utf_8_c_char_p_array_from_unicode_strings(
        ['%s=%s' % (name, value) for name, value in environment.items()]
    )
    executable_path_pointer = ctypes.c_char_p(executable_path.encode('utf-8'))

    process_id = proclaunch.fork()
    if process_id < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))

    if process_id == 0:
        try:
            if LIBC_SETSID() < 0:
                error_number = ctypes.get_errno()
                sys.stderr.write('setsid failed: %s\n' % os.strerror(error_number))
                proclaunch._exit(1)
            if LIBC_IOCTL(slave_file_descriptor, termios.TIOCSCTTY, 0) < 0:
                error_number = ctypes.get_errno()
                sys.stderr.write('TIOCSCTTY failed: %s\n' % os.strerror(error_number))
                proclaunch._exit(1)
            if proclaunch.dup2(slave_file_descriptor, 0) < 0:
                error_number = ctypes.get_errno()
                sys.stderr.write('dup2 stdin failed: %s\n' % os.strerror(error_number))
                proclaunch._exit(1)
            if proclaunch.dup2(slave_file_descriptor, 1) < 0:
                error_number = ctypes.get_errno()
                sys.stderr.write('dup2 stdout failed: %s\n' % os.strerror(error_number))
                proclaunch._exit(1)
            if proclaunch.dup2(slave_file_descriptor, 2) < 0:
                error_number = ctypes.get_errno()
                sys.stderr.write('dup2 stderr failed: %s\n' % os.strerror(error_number))
                proclaunch._exit(1)
            if slave_file_descriptor not in [0, 1, 2]:
                proclaunch.close(slave_file_descriptor)
            proclaunch.execve(executable_path_pointer, argument_array, environment_array)
            error_number = ctypes.get_errno()
            sys.stderr.write('execve failed: %s\n' % os.strerror(error_number))
            proclaunch._exit(1)
        except Exception as error:
            sys.stderr.write(str(error) + '\n')
            proclaunch._exit(1)

    return process_id


def wait_for_exit_code(process_id):
    # type: (int) -> int
    wait_status = ctypes.c_int()
    while True:
        result = proclaunch.waitpid(process_id, ctypes.byref(wait_status), 0)
        if result < 0:
            error_number = ctypes.get_errno()
            if error_number == 4:
                continue
            raise OSError(error_number, os.strerror(error_number))
        break
    if proclaunch.WIFEXITED(wait_status.value):
        return proclaunch.WEXITSTATUS(wait_status.value)
    if proclaunch.WIFSIGNALED(wait_status.value):
        return 128 + proclaunch.WTERMSIG(wait_status.value)
    return 1


def set_winsize(file_descriptor, width, height, pixelwidth, pixelheight):
    # type: (int, int, int, int, int) -> None
    window_size_bytes = struct.pack('HHHH', height, width, pixelheight, pixelwidth)
    fcntl.ioctl(file_descriptor, termios.TIOCSWINSZ, window_size_bytes)


class LocalSFTPHandle(paramiko.SFTPHandle):
    __slots__ = ()

    def stat(self):
        # type: () -> Union[paramiko.SFTPAttributes, int]
        try:
            if hasattr(self, 'readfile'):
                return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.writefile.fileno()))
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def chattr(self, attr):
        # type: (paramiko.SFTPAttributes) -> int
        try:
            paramiko.SFTPServer.set_file_attr(self._get_name(), attr)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)


class SessionState(object):
    __slots__ = (
        'pty_requested',
        'term',
        'width',
        'height',
        'width_pixels',
        'height_pixels',
        'shell_requested',
        'exec_command',
        'subsystem',
        'request_event',
        'pty_master_fd',
    )

    def __init__(self):
        # type: () -> None
        self.pty_requested = False
        self.term = 'xterm-256color'
        self.width = 80
        self.height = 24
        self.width_pixels = 0
        self.height_pixels = 0
        self.shell_requested = False
        self.exec_command = None  # type: Optional[str]
        self.subsystem = None  # type: Optional[str]
        self.request_event = threading.Event()
        self.pty_master_fd = None  # type: Optional[int]


class RemoteForwardListener(object):
    __slots__ = ('transport', 'bind_addr', 'bind_port', 'sock', 'bound_port', 'closed_event', 'thread')

    def __init__(self, transport, bind_addr, bind_port):
        # type: (paramiko.Transport, str, int) -> None
        self.transport = transport
        self.bind_addr = bind_addr if bind_addr not in ('', None) else '0.0.0.0'
        self.bind_port = bind_port
        if ':' in self.bind_addr:
            family = socket.AF_INET6
        else:
            family = socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.bind_addr, self.bind_port))
        self.sock.listen(DEFAULT_LISTEN_BACKLOG)
        self.bound_port = self.sock.getsockname()[1]
        self.closed_event = threading.Event()
        self.thread = threading.Thread(target=self.run_loop)
        self.thread.daemon = True
        self.thread.start()
        LOGGER.info('[reverse] listening on %s:%s' % (self.bind_addr, self.bound_port))

    def run_loop(self):
        # type: () -> None
        while not self.closed_event.is_set() and self.transport.is_active():
            try:
                readable_list, unused_writable_list, unused_error_list = select.select([self.sock], [], [], DEFAULT_SELECT_TIMEOUT_SECONDS)
                if not readable_list:
                    continue
                client_socket, client_address = self.sock.accept()
            except Exception:
                if not self.closed_event.is_set():
                    time.sleep(0.1)
                continue
            worker_thread = threading.Thread(target=self.handle_client, args=(client_socket, client_address))
            worker_thread.daemon = True
            worker_thread.start()

    def handle_client(self, client_socket, client_address):
        # type: (socket.socket, Any) -> None
        try:
            channel = self.transport.open_forwarded_tcpip_channel(
                client_address,
                (self.bind_addr, self.bound_port),
            )
            if channel is None:
                client_socket.close()
                return
            bridge_socket_and_channel(client_socket, channel)
        except Exception:
            try:
                client_socket.close()
            except Exception:
                pass

    def close(self):
        # type: () -> None
        self.closed_event.set()
        try:
            self.sock.close()
        except Exception:
            pass


class SSHShareServer(paramiko.ServerInterface):
    __slots__ = (
        'login_username_text',
        'login_password_text',
        'transport',
        'sessions',
        'direct_tcpip',
        'reverse_forwards',
        'lock',
    )

    def __init__(self, login_username_text, login_password_text, transport):
        # type: (str, str, paramiko.Transport) -> None
        self.login_username_text = login_username_text
        self.login_password_text = login_password_text
        self.transport = transport
        self.sessions = {}  # type: Dict[int, SessionState]
        self.direct_tcpip = {}  # type: Dict[int, Tuple[Tuple[str, int], Tuple[str, int]]]
        self.reverse_forwards = {}  # type: Dict[Tuple[str, int], RemoteForwardListener]
        self.lock = threading.Lock()

    def check_auth_password(self, username, password):
        # type: (str, str) -> int
        if username == self.login_username_text and password == self.login_password_text:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        # type: (str) -> str
        return 'password'

    def check_channel_request(self, kind, chanid):
        # type: (str, int) -> int
        if kind == SESSION_CHANNEL_KIND:
            with self.lock:
                self.sessions[chanid] = SessionState()
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        # type: (int, Tuple[str, int], Tuple[str, int]) -> int
        with self.lock:
            self.direct_tcpip[chanid] = (origin, destination)
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        # type: (paramiko.Channel, str, int, int, int, int, Any) -> bool
        state = self.sessions.get(channel.chanid)
        if state is None:
            return False
        state.pty_requested = True
        state.term = term
        state.width = width
        state.height = height
        state.width_pixels = pixelwidth
        state.height_pixels = pixelheight
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        # type: (paramiko.Channel, int, int, int, int) -> bool
        state = self.sessions.get(channel.chanid)
        if state is None:
            return False
        state.width = width
        state.height = height
        state.width_pixels = pixelwidth
        state.height_pixels = pixelheight
        if state.pty_master_fd is not None:
            set_winsize(state.pty_master_fd, width, height, pixelwidth, pixelheight)
        return True

    def check_channel_env_request(self, channel, name, value):
        # type: (paramiko.Channel, str, str) -> bool
        return False

    def check_channel_shell_request(self, channel):
        # type: (paramiko.Channel) -> bool
        state = self.sessions.get(channel.chanid)
        if state is None:
            return False
        state.shell_requested = True
        state.request_event.set()
        return True

    def check_channel_exec_request(self, channel, command):
        # type: (paramiko.Channel, str) -> bool
        state = self.sessions.get(channel.chanid)
        if state is None:
            return False
        state.exec_command = command
        state.request_event.set()
        return True

    def check_channel_subsystem_request(self, channel, name):
        # type: (paramiko.Channel, str) -> bool
        accepted = paramiko.ServerInterface.check_channel_subsystem_request(self, channel, name)
        state = self.sessions.get(channel.chanid)
        if state is not None:
            state.subsystem = name
            state.request_event.set()
        return accepted

    def check_port_forward_request(self, address, port):
        # type: (str, int) -> Union[bool, int]
        bind_addr_text = address
        if bind_addr_text == '':
            bind_addr_text = '0.0.0.0'
        try:
            listener = RemoteForwardListener(self.transport, bind_addr_text, port)
        except OSError:
            LOGGER.exception('[reverse] failed to listen on %s:%s' % (bind_addr_text, port))
            return False
        key = (bind_addr_text, listener.bound_port)
        with self.lock:
            self.reverse_forwards[key] = listener
        return listener.bound_port

    def cancel_port_forward_request(self, address, port):
        # type: (str, int) -> None
        bind_addr_text = address
        if bind_addr_text == '':
            bind_addr_text = '0.0.0.0'
        key = (bind_addr_text, port)
        with self.lock:
            listener = self.reverse_forwards.pop(key, None)
        if listener is not None:
            listener.close()
            LOGGER.info('[reverse] stopped %s:%s' % (bind_addr_text, port))

    def get_session(self, chanid):
        # type: (int) -> Optional[SessionState]
        with self.lock:
            return self.sessions.get(chanid)

    def remove_session(self, chanid):
        # type: (int) -> None
        with self.lock:
            self.sessions.pop(chanid, None)

    def pop_direct_tcpip(self, chanid):
        # type: (int) -> Optional[Tuple[Tuple[str, int], Tuple[str, int]]]
        with self.lock:
            return self.direct_tcpip.pop(chanid, None)

    def close(self):
        # type: () -> None
        with self.lock:
            listeners = list(self.reverse_forwards.values())
            self.reverse_forwards = {}
        for listener in listeners:
            listener.close()


class SSHShareSFTPServer(paramiko.SFTPServerInterface):
    __slots__ = ()

    def __init__(self, server):
        # type: (SSHShareServer) -> None
        paramiko.SFTPServerInterface.__init__(self, server)

    def canonicalize(self, path):
        # type: (str) -> str
        return path

    def list_folder(self, path):
        # type: (str) -> Union[List[paramiko.SFTPAttributes], int]
        real_path_value = path
        try:
            attribute_list = []  # type: List[paramiko.SFTPAttributes]
            for entry_name_value in os.listdir(real_path_value):
                entry_path_value = os.path.join(real_path_value, entry_name_value)
                attributes = paramiko.SFTPAttributes.from_stat(os.lstat(entry_path_value))
                attributes.filename = entry_name_value
                attribute_list.append(attributes)
            return attribute_list
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def stat(self, path):
        # type: (str) -> Union[paramiko.SFTPAttributes, int]
        real_path_value = path
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(real_path_value))
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def lstat(self, path):
        # type: (str) -> Union[paramiko.SFTPAttributes, int]
        real_path_value = path
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(real_path_value))
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def open(self, path, flags, attr):
        # type: (str, int, Optional[paramiko.SFTPAttributes]) -> Union[LocalSFTPHandle, int]
        real_path_value = path
        file_mode = getattr(attr, 'st_mode', None)
        if file_mode is None:
            file_mode = 438
        try:
            file_descriptor = os.open(real_path_value, flags, file_mode)
            if (flags & os.O_RDWR) == os.O_RDWR:
                python_mode_text = 'a+b' if (flags & os.O_APPEND) else 'r+b'
            elif flags & os.O_WRONLY:
                python_mode_text = 'ab' if (flags & os.O_APPEND) else 'wb'
            else:
                python_mode_text = 'rb'
            file_object = os.fdopen(file_descriptor, python_mode_text, 0)
            handle = LocalSFTPHandle(flags)
            handle._set_name(real_path_value)
            if (flags & os.O_WRONLY) == 0 or (flags & os.O_RDWR):
                handle.readfile = file_object
            if flags & os.O_WRONLY or (flags & os.O_RDWR):
                handle.writefile = file_object
            return handle
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def remove(self, path):
        # type: (str) -> int
        real_path_value = path
        try:
            os.remove(real_path_value)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def rename(self, oldpath, newpath):
        # type: (str, str) -> int
        try:
            os.rename(oldpath, newpath)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def posix_rename(self, oldpath, newpath):
        # type: (str, str) -> int
        return self.rename(oldpath, newpath)

    def mkdir(self, path, attr):
        # type: (str, Optional[paramiko.SFTPAttributes]) -> int
        file_mode = getattr(attr, 'st_mode', 511) if attr is not None else 511
        try:
            os.mkdir(path, file_mode)
            if attr is not None:
                paramiko.SFTPServer.set_file_attr(path, attr)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def rmdir(self, path):
        # type: (str) -> int
        try:
            os.rmdir(path)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def chattr(self, path, attr):
        # type: (str, paramiko.SFTPAttributes) -> int
        try:
            paramiko.SFTPServer.set_file_attr(path, attr)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)



def bridge_socket_and_channel(sock, channel):
    # type: (socket.socket, paramiko.Channel) -> None
    def pump_socket_to_channel():
        # type: () -> None
        try:
            while True:
                data = sock.recv(32768)
                if not data:
                    break
                channel.sendall(data)
        except Exception:
            pass
        finally:
            try:
                channel.shutdown_write()
            except Exception:
                pass

    def pump_channel_to_socket():
        # type: () -> None
        try:
            while True:
                data = channel.recv(32768)
                if not data:
                    break
                sock.sendall(data)
        except Exception:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    left_thread = threading.Thread(target=pump_socket_to_channel)
    right_thread = threading.Thread(target=pump_channel_to_socket)
    left_thread.daemon = True
    right_thread.daemon = True
    left_thread.start()
    right_thread.start()
    left_thread.join()
    right_thread.join()
    try:
        channel.close()
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


def handle_direct_tcpip_channel(channel, origin, destination):
    # type: (paramiko.Channel, Tuple[str, int], Tuple[str, int]) -> None
    try:
        if ':' in destination[0]:
            family = socket.AF_INET6
        else:
            family = socket.AF_INET
        upstream_socket = socket.socket(family, socket.SOCK_STREAM)
        upstream_socket.connect((destination[0], destination[1]))
        LOGGER.info('[forward] %s:%s -> %s:%s' % (origin[0], origin[1], destination[0], destination[1]))
        bridge_socket_and_channel(upstream_socket, channel)
    except Exception:
        LOGGER.exception('[forward] failed %s:%s -> %s:%s' % (origin[0], origin[1], destination[0], destination[1]))
        try:
            channel.close()
        except Exception:
            pass


def relay_channel_to_file_descriptor(channel, file_descriptor):
    # type: (paramiko.Channel, int) -> None
    try:
        while True:
            data = channel.recv(32768)
            if not data:
                break
            os.write(file_descriptor, data)
    except Exception:
        pass
    finally:
        try:
            os.close(file_descriptor)
        except Exception:
            pass


def relay_file_descriptor_to_channel(file_descriptor, channel):
    # type: (int, paramiko.Channel) -> None
    try:
        while True:
            readable_list, unused_writable_list, unused_error_list = select.select([file_descriptor], [], [], DEFAULT_SELECT_TIMEOUT_SECONDS)
            if not readable_list:
                if channel.closed:
                    break
                continue
            data = os.read(file_descriptor, 32768)
            if not data:
                break
            channel.sendall(data)
    except Exception:
        pass
    finally:
        try:
            channel.shutdown_write()
        except Exception:
            pass


def pump_stream_file_descriptor_to_channel(file_descriptor, send_function):
    # type: (int, Callable[[bytes], Any]) -> None
    try:
        while True:
            data = os.read(file_descriptor, 32768)
            if not data:
                break
            send_function(data)
    except Exception:
        pass
    finally:
        try:
            os.close(file_descriptor)
        except Exception:
            pass


def run_pty_command(channel, state, command_text):
    # type: (paramiko.Channel, SessionState, Optional[str]) -> None
    shell_text = get_unicode_shell()
    if command_text is None:
        arguments = [shell_text, '-i']
    else:
        arguments = [shell_text, '-lc', command_text]
    master_file_descriptor, slave_file_descriptor = pty.openpty()
    state.pty_master_fd = master_file_descriptor
    set_winsize(master_file_descriptor, state.width, state.height, state.width_pixels, state.height_pixels)
    process_id = None  # type: Optional[int]
    try:
        process_id = launch_pty_process(arguments, slave_file_descriptor)
    finally:
        try:
            os.close(slave_file_descriptor)
        except Exception:
            pass

    input_thread = threading.Thread(target=relay_channel_to_file_descriptor, args=(channel, os.dup(master_file_descriptor)))
    output_thread = threading.Thread(target=relay_file_descriptor_to_channel, args=(os.dup(master_file_descriptor), channel))
    input_thread.daemon = True
    output_thread.daemon = True
    input_thread.start()
    output_thread.start()
    exit_code = wait_for_exit_code(process_id)
    input_thread.join(1.0)
    output_thread.join(1.0)
    try:
        channel.send_exit_status(exit_code)
    except Exception:
        pass
    try:
        os.close(master_file_descriptor)
    except Exception:
        pass
    state.pty_master_fd = None
    try:
        channel.close()
    except Exception:
        pass


def run_pipe_command(channel, state, command_text):
    # type: (paramiko.Channel, SessionState, Optional[str]) -> None
    shell_text = get_unicode_shell()
    if command_text is None:
        arguments = [shell_text, '-i']
    else:
        arguments = [shell_text, '-lc', command_text]

    stdin_read_file_descriptor, stdin_write_file_descriptor = proclaunch.create_pipe()
    stdout_read_file_descriptor, stdout_write_file_descriptor = proclaunch.create_pipe()
    stderr_read_file_descriptor, stderr_write_file_descriptor = proclaunch.create_pipe()

    process_id = None  # type: Optional[int]
    try:
        process_id = proclaunch.launch(
            arguments,
            None,
            stdin_read_file_descriptor,
            stdout_write_file_descriptor,
            stderr_write_file_descriptor,
        )
    finally:
        try:
            os.close(stdin_read_file_descriptor)
        except Exception:
            pass
        try:
            os.close(stdout_write_file_descriptor)
        except Exception:
            pass
        try:
            os.close(stderr_write_file_descriptor)
        except Exception:
            pass

    def channel_to_stdin():
        # type: () -> None
        try:
            while True:
                data = channel.recv(32768)
                if not data:
                    break
                os.write(stdin_write_file_descriptor, data)
        except Exception:
            pass
        finally:
            try:
                os.close(stdin_write_file_descriptor)
            except Exception:
                pass

    threads = [
        threading.Thread(target=channel_to_stdin),
        threading.Thread(target=pump_stream_file_descriptor_to_channel, args=(stdout_read_file_descriptor, channel.sendall)),
        threading.Thread(target=pump_stream_file_descriptor_to_channel, args=(stderr_read_file_descriptor, channel.send_stderr)),
    ]
    for thread_object in threads:
        thread_object.daemon = True
        thread_object.start()
    exit_code = wait_for_exit_code(process_id)
    for thread_object in threads:
        thread_object.join(1.0)
    try:
        channel.send_exit_status(exit_code)
    except Exception:
        pass
    try:
        channel.close()
    except Exception:
        pass


def handle_session_channel(channel, state, server):
    # type: (paramiko.Channel, SessionState, SSHShareServer) -> None
    try:
        if not state.request_event.wait(DEFAULT_CHANNEL_WAIT_TIMEOUT_SECONDS):
            try:
                channel.close()
            except Exception:
                pass
            return
        if state.subsystem == SFTP_SUBSYSTEM_NAME:
            return
        command_text = state.exec_command.decode('utf-8')
        if state.pty_requested:
            run_pty_command(channel, state, command_text)
        else:
            run_pipe_command(channel, state, command_text)
    except Exception as error:
        LOGGER.exception('[session] channel %s failed' % channel.chanid)
        try:
            channel.send_stderr(('server error: %s\n' % str(error)).encode('utf-8'))
        except Exception:
            pass
        try:
            channel.send_exit_status(1)
        except Exception:
            pass
        try:
            channel.close()
        except Exception:
            pass
    finally:
        server.remove_session(channel.chanid)


def load_host_key(host_key_path, host_key_passphrase_text):
    # type: (str, Optional[str]) -> paramiko.PKey
    return paramiko.Ed25519Key.from_private_key_file(
        host_key_path,
        password=host_key_passphrase_text,
    )


def open_listen_socket(host_text, port_int):
    # type: (str, int) -> socket.socket
    address_info_list = socket.getaddrinfo(host_text, port_int, socket.AF_UNSPEC, socket.SOCK_STREAM)
    last_error = None  # type: Optional[Exception]
    for family, socket_type, protocol, unused_canonname, sockaddr in address_info_list:
        listen_socket = None  # type: Optional[socket.socket]
        try:
            listen_socket = socket.socket(family, socket_type, protocol)
            listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listen_socket.bind(sockaddr)
            listen_socket.listen(DEFAULT_LISTEN_BACKLOG)
            return listen_socket
        except Exception as error:
            last_error = error
            if listen_socket is not None:
                listen_socket.close()
    if last_error is None:
        raise OSError('socket.getaddrinfo returned no usable addresses')
    raise last_error


def handle_client_connection(client_socket, client_address, login_username_text, login_password_text, host_key):
    # type: (socket.socket, Any, str, str, paramiko.PKey) -> None
    transport = paramiko.Transport(client_socket)
    transport.add_server_key(host_key)
    transport.set_subsystem_handler(SFTP_SUBSYSTEM_NAME, paramiko.SFTPServer, SSHShareSFTPServer)
    server = SSHShareServer(login_username_text, login_password_text, transport)
    if isinstance(client_address, tuple) and len(client_address) >= 2:
        client_address_text = '%s:%s' % (client_address[0], client_address[1])
    else:
        client_address_text = str(client_address)
    try:
        transport.start_server(server=server)
        LOGGER.info('[client] connected %s' % client_address_text)
        while transport.is_active():
            channel = transport.accept(DEFAULT_SELECT_TIMEOUT_SECONDS)
            if channel is None:
                continue
            direct_tcpip_entry = server.pop_direct_tcpip(channel.chanid)
            if direct_tcpip_entry is not None:
                origin, destination = direct_tcpip_entry
                worker_thread = threading.Thread(target=handle_direct_tcpip_channel, args=(channel, origin, destination))
                worker_thread.daemon = True
                worker_thread.start()
                continue
            session_state = server.get_session(channel.chanid)
            if session_state is not None:
                worker_thread = threading.Thread(
                    target=handle_session_channel,
                    args=(channel, session_state, server),
                )
                worker_thread.daemon = True
                worker_thread.start()
                continue
            try:
                channel.close()
            except Exception:
                pass
    except Exception:
        LOGGER.exception('[client] %s error' % client_address_text)
    finally:
        server.close()
        try:
            transport.close()
        except Exception:
            pass
        try:
            client_socket.close()
        except Exception:
            pass
        LOGGER.info('[client] disconnected %s' % client_address_text)


def build_argument_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(description='Userland non-daemon SSH server inheriting the current user privileges, with shell, exec, SFTP, and TCP forwarding. POSIX only.')
    parser.add_argument('--username', type=str, required=True, help='Login username accepted by this SSH server.')
    parser.add_argument('--password', type=str, required=True, help='Login password accepted by this SSH server.')
    parser.add_argument('--host', type=str, default=DEFAULT_HOST, help='Host or IP address to bind.')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='TCP port to listen on.')
    parser.add_argument('--host-key', type=str, required=True, help='Path to an Ed25519 private host key file.')
    parser.add_argument('--host-key-passphrase', type=str, default=None, help='Passphrase for --host-key if the Ed25519 private key is encrypted.')
    return parser


def main():
    # type: () -> int
    arguments = build_argument_parser().parse_args()

    host_key_path = arguments.host_key  # type: str
    host_key_passphrase_text = arguments.host_key_passphrase  # type: Optional[str]

    host_key = load_host_key(host_key_path, host_key_passphrase_text)
    listen_socket = open_listen_socket(arguments.host, arguments.port)

    LOGGER.info('Listening on %s:%s' % (arguments.host, arguments.port))
    LOGGER.info('Login username: %s' % arguments.username)
    LOGGER.info('SFTP root: /')
    LOGGER.info('Process working directory: %s' % os.getcwd())
    LOGGER.info('Features: shell, exec, PTY, SFTP, direct TCP forwarding, reverse TCP forwarding')
    LOGGER.info('Host key: %s' % host_key_path)

    try:
        while True:
            client_socket, client_address = listen_socket.accept()
            worker_thread = threading.Thread(
                target=handle_client_connection,
                args=(client_socket, client_address, arguments.username, arguments.password, host_key),
            )
            worker_thread.daemon = True
            worker_thread.start()
    except KeyboardInterrupt:
        LOGGER.info('Shutting down')
    finally:
        listen_socket.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
