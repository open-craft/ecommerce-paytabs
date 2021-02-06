# -*- coding: utf-8 -*-
"""
Paytabs Processor Django application initialization.
"""

from __future__ import absolute_import, unicode_literals

from django.apps import AppConfig


class PayTabsConfig(AppConfig):
    """
    Configuration for the PayTabs Processor Django application.
    """

    name = 'paytabs'
    plugin_app = {
        'url_config': {
            'ecommerce': {
                'namespace': 'paytabs',
            },
        },
    }
