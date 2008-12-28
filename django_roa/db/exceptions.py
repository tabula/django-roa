from django.conf import settings
from django.utils.html import strip_tags

ROA_DJANGO_ERRORS = getattr(settings, 'ROA_DJANGO_ERRORS', False)

class ROAException(Exception):
    def __init__(self, exception):
        if ROA_DJANGO_ERRORS:
            self.message = exception.message
            self.status_code = exception.status_code
        else:
            self.message = str(exception)

    def __str__(self):
        return ROA_DJANGO_ERRORS and self.parse_django_error() or self.message
    
    def parse_django_error(self):
        """Extract the summary part of a Django HTML error."""
        summary = self.message.split('<body>\n<div id="summary">\n  ', 1)[1]\
                              .split('<th>Python Executable:</th>', 1)[0]
        result = []
        title = None
        for line in strip_tags(summary).split('\n'):
            line_content = line.strip()
            if line_content:
                if line_content.endswith(':'):
                    title = line_content
                elif title is None:
                    title = "%s:" % line_content
                else:
                    result.append("%s %s\n" % (title, line_content))
        result.append("Status code: %s" % self.status_code)
        return " ".join(result)

