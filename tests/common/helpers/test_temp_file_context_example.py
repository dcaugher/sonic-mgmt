"""
Example usage of the temporary file context managers.
""" 

import pytest
from tests.common.helpers.temp_file_context import temporary_file, temporary_files


def test_example_single_temporary_file(duthost):
    """
    Example test showing how to use the temporary_file context manager.
    """
    # Test using single temporary file
    with temporary_file(duthost) as tmpfile:
        # Write some test data to the temporary file
        test_content = "This is test content for temporary file"
        duthost.shell(f"echo '{test_content}' > {tmpfile}")
        
        # Read back the content to verify
        result = duthost.shell(f"cat {tmpfile}")
        assert test_content in result['stdout']
        
        # File path should exist while in context
        file_check = duthost.shell(f"test -f {tmpfile}")
        assert file_check['rc'] == 0
    
    # After exiting context, file should be deleted
    file_check = duthost.shell(f"test -f {tmpfile}", module_ignore_errors=True)
    assert file_check['rc'] != 0  # File should not exist


def test_example_multiple_temporary_files(duthost):
    """
    Example test showing how to use the temporary_files context manager.
    """
    # Test using multiple temporary files
    with temporary_files(duthost, count=3) as tmpfiles:
        assert len(tmpfiles) == 3
        
        # Use each temporary file
        for i, tmpfile in enumerate(tmpfiles):
            test_content = f"Content for file {i+1}"
            duthost.shell(f"echo '{test_content}' > {tmpfile}")
            
            # Verify content
            result = duthost.shell(f"cat {tmpfile}")
            assert test_content in result['stdout']
            
            # Verify file exists
            file_check = duthost.shell(f"test -f {tmpfile}")
            assert file_check['rc'] == 0
    
    # After exiting context, all files should be deleted
    for tmpfile in tmpfiles:
        file_check = duthost.shell(f"test -f {tmpfile}", module_ignore_errors=True)
        assert file_check['rc'] != 0  # File should not exist


def test_example_json_config_with_temporary_file(duthost):
    """
    Example showing how to use temporary file for JSON configuration.
    """
    import json
    
    config_data = {
        "test_config": {
            "enabled": True,
            "timeout": 30,
            "retries": 3
        }
    }
    
    with temporary_file(duthost) as tmpfile:
        # Write JSON config to temporary file
        json_content = json.dumps(config_data, indent=2)
        duthost.shell(f"cat > {tmpfile} << 'EOF'\n{json_content}\nEOF")
        
        # Read back and parse JSON
        result = duthost.shell(f"cat {tmpfile}")
        parsed_config = json.loads(result['stdout'])
        
        # Verify the configuration
        assert parsed_config == config_data
        assert parsed_config['test_config']['enabled'] is True
        assert parsed_config['test_config']['timeout'] == 30


def test_example_error_handling_with_temporary_file(duthost):
    """
    Example showing that temporary files are cleaned up even when exceptions occur.
    """
    tmpfile_path = None
    
    try:
        with temporary_file(duthost) as tmpfile:
            tmpfile_path = tmpfile
            
            # Write some content
            duthost.shell(f"echo 'test content' > {tmpfile}")
            
            # Verify file exists
            file_check = duthost.shell(f"test -f {tmpfile}")
            assert file_check['rc'] == 0
            
            # Simulate an error
            raise ValueError("Simulated error for testing cleanup")
            
    except ValueError as e:
        assert str(e) == "Simulated error for testing cleanup"
        
        # File should still be cleaned up despite the exception
        file_check = duthost.shell(f"test -f {tmpfile_path}", module_ignore_errors=True)
        assert file_check['rc'] != 0  # File should not exist