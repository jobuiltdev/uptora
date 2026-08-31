"""Flow executors, one module per flow kind.

Adding LOGIN or SEARCH later means adding a module here plus a plan builder and
one branch in flow.run_flow_check. Nothing in the monitoring engine changes.
"""

from monitors.checks.flows.contact_form import ContactFormPlan, FieldAction, run_contact_form

__all__ = ['ContactFormPlan', 'FieldAction', 'run_contact_form']
