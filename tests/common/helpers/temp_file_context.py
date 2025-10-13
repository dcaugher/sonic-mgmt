"""
Context manager for temporary file handling in sonic-mgmt tests.
"""

import logging
from contextlib import contextmanager
from tests.common.gu_utils import generate_tmpfile, delete_tmpfile

logger = logging.getLogger(__name__)


@contextmanager
def temporary_file(duthost):
    """
    Context manager for creating and cleaning up temporary files on DUT.
    
    Args:
        duthost: DUT host object
    
    Yields:
        str: Path to the temporary file
        
    Example:
        with temporary_file(duthost) as tmpfile:
            # Use tmpfile for operations
            duthost.shell(f"echo 'test content' > {tmpfile}")
            content = duthost.shell(f"cat {tmpfile}")
            # File is automatically deleted when exiting the context
    """
    tmpfile = None
    try:
        logger.debug("Creating temporary file")
        tmpfile = generate_tmpfile(duthost)
        logger.debug(f"Created temporary file: {tmpfile}")
        yield tmpfile
    except Exception as e:
        logger.error(f"Error during temporary file operation: {e}")
        raise
    finally:
        if tmpfile:
            try:
                logger.debug(f"Cleaning up temporary file: {tmpfile}")
                delete_tmpfile(duthost, tmpfile)
                logger.debug(f"Successfully deleted temporary file: {tmpfile}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up temporary file {tmpfile}: {cleanup_error}")


@contextmanager
def temporary_files(duthost, count=1):
    """
    Context manager for creating and cleaning up multiple temporary files on DUT.
    
    Args:
        duthost: DUT host object
        count (int): Number of temporary files to create
    
    Yields:
        list: List of paths to the temporary files
        
    Example:
        with temporary_files(duthost, count=2) as tmpfiles:
            file1, file2 = tmpfiles
            duthost.shell(f"echo 'config1' > {file1}")
            duthost.shell(f"echo 'config2' > {file2}")
            # Both files are automatically deleted when exiting the context
    """
    tmpfiles = []
    try:
        logger.debug(f"Creating {count} temporary files")
        for i in range(count):
            tmpfile = generate_tmpfile(duthost)
            tmpfiles.append(tmpfile)
            logger.debug(f"Created temporary file {i+1}/{count}: {tmpfile}")
        
        yield tmpfiles
    except Exception as e:
        logger.error(f"Error during temporary files operation: {e}")
        raise
    finally:
        for tmpfile in tmpfiles:
            try:
                logger.debug(f"Cleaning up temporary file: {tmpfile}")
                delete_tmpfile(duthost, tmpfile)
                logger.debug(f"Successfully deleted temporary file: {tmpfile}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up temporary file {tmpfile}: {cleanup_error}")