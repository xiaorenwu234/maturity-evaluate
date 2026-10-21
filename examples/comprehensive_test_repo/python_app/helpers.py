"""Helper functions for cross-file import testing."""


def format_data(data):
    """Format input data."""
    if isinstance(data, list):
        return [str(item).upper() for item in data]
    return str(data).upper()


def process_items(items):
    """Process formatted items."""
    result = []
    for item in items:
        if len(item) > 3:
            result.append(item[:3])
        else:
            result.append(item)
    return result


class DataProcessor:
    """Service class for data processing."""
    
    def __init__(self):
        self.cache = {}
    
    def process(self, data):
        """Main processing method."""
        formatted = format_data(data)
        processed = process_items(formatted)
        self.cache['last'] = processed
        return processed
    
    def get_cached(self):
        """Retrieve cached results."""
        return self.cache.get('last', [])
