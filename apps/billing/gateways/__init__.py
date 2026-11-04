"""Payment gateway clients.

A package rather than a module on ``apps.billing`` directly because the
models deliberately name their external-id columns ``gateway_*`` instead
of ``asaas_*`` — swapping or adding a gateway should mean a sibling file
here, not a migration.
"""
