from django import template

register = template.Library()

@register.filter
def format_duration(seconds):
    """Convert seconds to human-readable duration (MM:SS or HH:MM:SS)."""
    if seconds is None:
        return "-"
    
    try:
        seconds = int(seconds)
        if seconds < 0:
            return "-"
        
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"
    except (ValueError, TypeError):
        return "-"
