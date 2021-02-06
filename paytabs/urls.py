# -*- coding: utf-8 -*-
"""
Defines the URL routes for paytabs app.
"""

from __future__ import absolute_import, unicode_literals

from django.conf.urls import url

from .views import PayTabsResponseView

urlpatterns = [
    url(r'^paytabs/submit$', PayTabsResponseView.as_view(), name='paytabs-submit'),
]

