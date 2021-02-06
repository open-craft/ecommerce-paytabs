"""PayTabs payment processing."""

import logging

import pycountry
import requests
import six
from django.conf import settings
from django.urls import reverse
from django.utils.functional import cached_property
from ipware import get_client_ip
from oscar.apps.payment.exceptions import GatewayError

from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse

logger = logging.getLogger(__name__)


class PayTabs(BasePaymentProcessor):
    """
    PayTabs payment processor.

    For reference, see https://dev.paytabs.com/
    """

    NAME = 'paytabs'

    def __init__(self, site):
        super(PayTabs, self).__init__(site)
        configuration = self.configuration
        self.merchant_email = configuration['merchant_email']
        self.secret_key = configuration['secret_key']
        self.return_base_url = configuration['return_base_url']
        self.site = site

    def _get_user_profile_data(self, user, request):
        """
        Returns the profile data fields for the given user.
        """
        def get_extended_field(account_details, field_name, default_val=None):
            """Helper function to extract data from the extended profile."""
            return next(
                (
                    field.get('field_value', default_val) for field in account_details['extended_profile']
                    if field['field_name'] == field_name
                ),
                default_val
            )

        account_details = user.account_details(request)
        if account_details.get('country'):
            # convert two letter country code to the three expected by paytabs
            country_code = pycountry.countries.get(alpha_2=account_details.get('country')).alpha_3
        else:
            raise PayTabsException('Country must be set in user account settings')

        return {
            'first_name': get_extended_field(account_details, 'first_name'),
            'last_name': get_extended_field(account_details, 'last_name'),
            'mailing_address': get_extended_field(account_details, 'mailing_address'),
            'city': get_extended_field(account_details, 'city'),
            'country_code': country_code,
            'postal_code': get_extended_field(account_details, 'ZIP/Postal Code', '11564'),
            'state': get_extended_field(account_details, 'state'),
        }

    def _get_course_id_title(self, line):
        """
        Return the line title prefixed with the course ID, if available.
        """
        course_id = ''
        line_course = line.product.course
        if line_course:
            course_id = '{}|'.format(line_course.id)
        return course_id + line.product.title

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        """
        Return the transaction parameters needed for this processor.
        """
        site_url = '{}://{}'.format(request.scheme, request.get_host())

        # This can only be used in production when the requested site is publicly accessible on the internet
        # via HTTPS
        # return_url = request.build_absolute_uri(reverse('paytabs:submit'))
        return_url = '{}{}'.format(self.return_base_url, reverse('paytabs:paytabs-submit'))
        client_ip, is_routable = get_client_ip(request)
        ip_merchant = '-'
        ip_customer = client_ip if is_routable else '-'
        title = 'Order: {}'.format(basket.order_number)
        user = basket.owner
        user_profile_data = self._get_user_profile_data(user, request)
        products = {'names': [], 'quantities': [], 'prices': []}
        for line in basket.all_lines():
            products['names'].append(self._get_course_id_title(line))
            products['quantities'].append(six.text_type(line.quantity))
            products['prices'].append(six.text_type(line.line_price_incl_tax_incl_discounts / line.quantity))

        invoiced_products = {key: ' || '.join(val) for key, val in products.items()}
        other_charges = '0.0'
        amount = str(basket.total_incl_tax)
        discount = '0.0'
        currency = basket.currency
        reference_no = basket.order_number
        msg_lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) or settings.LANGUAGE_CODE
        cms_with_version = 'Open edX E-Commerce'

        pay_data = {
            'merchant_email': self.merchant_email,
            'secret_key': self.secret_key,
            'site_url': site_url,
            'return_url': return_url,
            'title': title,
            'cc_first_name': user_profile_data['first_name'],
            'cc_last_name': user_profile_data['last_name'],
            'cc_phone_number': self.configuration['default_phone_number_country_code'],
            'phone_number': 'Phone Number',
            'email': user.email,
            'products_per_title': invoiced_products['names'],
            'unit_price': invoiced_products['prices'],
            'quantity': invoiced_products['quantities'],
            'other_charges': other_charges,
            'amount': amount,
            'discount': discount,
            'currency': currency,
            'reference_no': reference_no,
            'ip_customer': ip_customer,
            'ip_merchant': ip_merchant,
            'billing_address': user_profile_data['mailing_address'],
            'city': user_profile_data['city'],
            'state': user_profile_data['state'],
            'postal_code': user_profile_data['postal_code'],
            'country': user_profile_data['country_code'],
            'shipping_first_name': user_profile_data['first_name'],
            'shipping_last_name': user_profile_data['last_name'],
            'address_shipping': user_profile_data['mailing_address'],
            'state_shipping': user_profile_data['state'],
            'city_shipping': user_profile_data['city'],
            'postal_code_shipping': user_profile_data['postal_code'],
            'country_shipping': user_profile_data['country_code'],
            'msg_lang': msg_lang,
            'cms_with_version': cms_with_version,
        }
        # Creation of Payment Page
        response = requests.post("https://www.paytabs.com/apiv2/create_pay_page", data=pay_data).json()
        if response.get('response_code') != '4012':
            err_msg = 'PayTabs raised an error when trying to process the payment (code: {response_code}, message: {result})'.format(**response)
            raise PayTabsException(err_msg)

        payment_url = response.get('payment_url')
        return {
            'payment_page_url': payment_url
        }

    def handle_processor_response(self, response, basket=None):
        """
        Handle the processor response.
        """
        if response['response_code'] != '100':
            raise PayTabsException('PayTabs raised an error when trying to process the payment')

        currency = response.get('currency')
        total = response.get('amount')
        transaction_id = response.get('transaction_id')
        card_first_six_digits = response.get('card_first_six_digits', 'XXXXXX')
        card_last_four_digits = response.get('card_last_four_digits', 'XXXX')
        card_number = '{}XXXXXX{}'.format(card_first_six_digits, card_last_four_digits)
        card_type = response.get('card_brand')
        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=card_number,
            card_type=card_type
        )

    def issue_credit(self, order_number, basket, reference_number, amount, currency):
        """
        This is currently not implemented.

        While PayTabs supports this (https://dev.paytabs.com/docs/refund/), this endpoint is not available
        for demo merchants.
        """
        logger.exception(
            'PayTabs processor cannot issue credits or refunds at the moment.'
        )


class PayTabsException(GatewayError):
    """
    An umbrella exception to catch all errors from PayTabs.
    """
    pass  # pylint: disable=unnecessary-pass
