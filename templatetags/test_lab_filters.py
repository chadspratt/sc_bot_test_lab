from django import template

register = template.Library()

@register.filter
def lookup(dictionary, key):
    """Template filter to look up a dictionary value by key."""
    if dictionary is None:
        return ''
    return dictionary.get(key, '')
