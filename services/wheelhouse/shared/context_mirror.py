import multiprocessing.shared_memory
import json
import logging
import time

logger = logging.getLogger(__name__)

class ContextMirror:
    """
    Unidirectional Shared Memory bridge for mirroring active window state.
    
    Writer: Input Process (Updates on focus change)
    Reader: Logic Process (Reads on demand)
    """
    
    SHM_NAME = "wheelhouse_context_mirror"
    SHM_SIZE = 4096  # 4KB buffer
    
    def __init__(self):
        self.shm = None
        self.is_writer = False

    def init_writer(self):
        """Initialize in writer mode (creates the shared memory)."""
        self.is_writer = True
        try:
            # Try to create, if exists, unlink and recreate
            try:
                self.shm = multiprocessing.shared_memory.SharedMemory(name=self.SHM_NAME, create=True, size=self.SHM_SIZE)
            except FileExistsError:
                self.shm = multiprocessing.shared_memory.SharedMemory(name=self.SHM_NAME)
                # Optional: unlink and recreate to ensure clean state, but attaching is faster if it's valid.
                # For robustness, let's just use it.
            
            # Initialize with empty JSON
            self.write_context({"app_name": "", "window_title": "", "timestamp": 0.0})
            logger.info(f"ContextMirror writer initialized: {self.SHM_NAME}")
        except Exception as e:
            logger.error(f"Failed to initialize ContextMirror writer: {e}")
            # Don't raise, just log. The system should survive without context.

    def init_reader(self):
        """Initialize in reader mode (attaches to existing shared memory)."""
        self.is_writer = False
        try:
            self.shm = multiprocessing.shared_memory.SharedMemory(name=self.SHM_NAME)
            logger.info(f"ContextMirror reader initialized: {self.SHM_NAME}")
        except FileNotFoundError:
            # This is expected if writer hasn't started yet
            logger.debug(f"ContextMirror shared memory '{self.SHM_NAME}' not found yet.")
            self.shm = None
        except Exception as e:
            logger.error(f"Failed to initialize ContextMirror reader: {e}")

    def write_context(self, ctx_dict: dict):
        """Write context dictionary to shared memory."""
        if not self.shm or not self.is_writer:
            return

        try:
            data = json.dumps(ctx_dict).encode('utf-8')
            if len(data) >= self.SHM_SIZE:
                logger.warning(f"Context data too large ({len(data)} bytes), truncating.")
                # Truncate safely? JSON will be broken. 
                # Better to write a safe empty/error dict or just the app name.
                data = json.dumps({"app_name": ctx_dict.get("app_name", ""), "window_title": "TRUNCATED", "timestamp": time.time()}).encode('utf-8')
            
            # Write data
            self.shm.buf[:len(data)] = data
            # Null terminate
            if len(data) < self.SHM_SIZE:
                self.shm.buf[len(data)] = 0
            
        except Exception as e:
            logger.error(f"Error writing context: {e}")

    def read_context(self) -> dict:
        """Read context dictionary from shared memory."""
        if not self.shm:
            # Try to reconnect
            if not self.is_writer:
                try:
                    self.init_reader()
                except:
                    pass
            if not self.shm:
                return {}

        try:
            # Read until null terminator
            buf_bytes = bytes(self.shm.buf)
            null_index = buf_bytes.find(b'\x00')
            if null_index != -1:
                data = buf_bytes[:null_index]
            else:
                data = buf_bytes
            
            if not data:
                return {}

            return json.loads(data.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Race condition or corruption
            return {}
        except Exception as e:
            logger.error(f"Error reading context: {e}")
            return {}

    def close(self):
        if self.shm:
            self.shm.close()
            if self.is_writer:
                try:
                    self.shm.unlink()
                except:
                    pass
