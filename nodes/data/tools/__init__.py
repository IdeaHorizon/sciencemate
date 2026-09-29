"""Register the Data service's small public capability surface."""

# Heavy domain implementations remain lazy behind these unified tools.
from . import lazy_domain_tools  # noqa: F401
from . import preprocessing_planner  # noqa: F401
from . import preprocessing_capabilities  # noqa: F401
from . import execute_preprocessing_plan  # noqa: F401
from . import scientific_mesh  # noqa: F401
