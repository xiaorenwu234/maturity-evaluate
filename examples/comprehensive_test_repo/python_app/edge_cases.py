"""Test file for edge cases: empty functions, nested classes, decorators."""


# Empty function
def empty_function():
    pass


# Function with complex control flow
def complex_control_flow(x, y):
    """Test CFG generation with multiple branches."""
    result = 0
    
    # If-else
    if x > 0:
        result += x
    else:
        result -= x
    
    # Nested if
    if y > 0:
        if y > 10:
            result *= 2
        else:
            result *= 3
    
    # While loop
    while result < 100:
        result += 1
        if result == 50:
            break
    
    # For loop
    for i in range(10):
        if i % 2 == 0:
            continue
        result += i
    
    # Try-except
    try:
        value = 1 / x
    except ZeroDivisionError:
        value = 0
    except Exception as e:
        value = -1
    finally:
        result += value
    
    return result


# Decorator usage
def my_decorator(func):
    def wrapper(*args, **kwargs):
        print(f"Calling {func.__name__}")
        return func(*args, **kwargs)
    return wrapper


@my_decorator
def decorated_function(x):
    """Function with decorator."""
    return x * 2


# Nested class
class OuterClass:
    """Outer class containing inner class."""
    
    def __init__(self):
        self.value = 10
    
    class InnerClass:
        """Nested inner class."""
        
        def __init__(self):
            self.inner_value = 20
        
        def inner_method(self):
            return self.inner_value
    
    def outer_method(self):
        inner = self.InnerClass()
        return inner.inner_method()


# Lambda and generator expressions
def lambda_and_generators(data):
    """Test lambda and generator handling."""
    # Lambda
    square = lambda x: x * x
    
    # Generator expression
    gen = (x * 2 for x in data if x > 0)
    
    # List comprehension with lambda
    result = list(map(lambda x: x + 1, data))
    
    return list(gen), result


# Dynamic calls (should not produce edges)
def dynamic_calls(obj):
    """Test dynamic dispatch - should NOT create CALLS edges."""
    method_name = "some_method"
    getattr(obj, method_name)()  # Dynamic - unresolvable
    
    # Also dynamic
    func = getattr(obj, "another_method", None)
    if func:
        func()


# Multiple inheritance
class BaseA:
    def method_a(self):
        return "A"


class BaseB:
    def method_b(self):
        return "B"


class MultiInherit(BaseA, BaseB):
    """Class with multiple inheritance."""
    
    def method_c(self):
        # Call parent methods
        a = self.method_a()
        b = self.method_b()
        return a + b


# Cross-file imports (will be tested with other files)
from . import helpers
from .helpers import format_data, process_items


def use_imports(data):
    """Test cross-file imports."""
    formatted = format_data(data)
    processed = process_items(formatted)
    return processed
