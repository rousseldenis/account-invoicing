# -*- coding: utf-8 -*-
# © 2015-2016 Akretion (Alexis de Lattre <alexis.delattre@akretion.com>)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from openerp import models, fields, api, _
from openerp.exceptions import Warning as UserError
from openerp.tools import float_compare
from datetime import datetime
import logging
import PyPDF2
from lxml import etree
from StringIO import StringIO

logger = logging.getLogger(__name__)


class AccountInvoiceImport(models.TransientModel):
    _inherit = 'account.invoice.import'

    @api.model
    def parse_pdf_invoice(self, file_data):
        zugferd_xml_root = False
        try:
            zugferd_xml_root = self._check_zugferd_pdf(file_data)
        except:
            pass
        if zugferd_xml_root:
            return self.parse_zugferd_xml(zugferd_xml_root)
        else:
            return super(AccountInvoiceImport, self).parse_pdf_invoice(
                file_data)

    def _check_zugferd_pdf(self, file_data):
        logger.info('Trying to find an embedded XML file inside PDF')
        fd = StringIO(file_data)
        pdf = PyPDF2.PdfFileReader(fd)
        logger.debug('pdf.trailer=%s', pdf.trailer)
        pdf_root = pdf.trailer['/Root']
        logger.debug('pdf_root=%s', pdf_root)
        embeddedfiles = pdf_root['/Names']['/EmbeddedFiles']['/Names']
        zugferd_file_dict_obj = False
        i = 0
        for embeddedfile in embeddedfiles[:-1]:
            if embeddedfile == 'ZUGFeRD-invoice.xml':
                zugferd_file_dict_obj = embeddedfiles[i+1]
                break
            i += 1
        if not zugferd_file_dict_obj:
            logger.info('No embedded file ZUGFeRD-invoice.xml')
            return False
        zugferd_file_dict = zugferd_file_dict_obj.getObject()
        logger.debug('zugferd_file_dict=%s', zugferd_file_dict)
        xml_string = zugferd_file_dict['/EF']['/F'].getData()
        xml_root = etree.fromstring(xml_string)
        logger.info('A valid XML file has been found in the PDF file')
        logger.debug(etree.tostring(
            xml_root, pretty_print=True, encoding='UTF-8',
            xml_declaration=True))
        return xml_root

    @api.model
    def parse_xml_invoice(self, xml_root):
        if (
                xml_root.tag and
                xml_root.tag.startswith(
                '{urn:ferd:CrossIndustryDocument:invoice:1p0')):
            return self.parse_zugferd_xml(xml_root)
        else:
            return super(AccountInvoiceImport, self).parse_xml_invoice(
                xml_root)

    @api.model
    def select_taxes_of_invoice_line(
            self, taxes_xpath, namespaces, unece2odoo_tax, line_name=False):
        '''This method is designed to be inherited'''
        tax_ids = []
        prec = self.env['decimal.precision'].precision_get('Account')
        for tax in taxes_xpath:
            type_code_xpath = tax.xpath("ram:TypeCode", namespaces=namespaces)
            type_code = type_code_xpath and type_code_xpath[0].text or 'VAT'
            # CategoryCode not available at Basic level
            categ_code_xpath = tax.xpath(
                "ram:CategoryCode", namespaces=namespaces)
            categ_code = categ_code_xpath and categ_code_xpath[0].text or False
            percent_xpath = tax.xpath(
                "ram:ApplicablePercent", namespaces=namespaces)
            percent = percent_xpath[0].text and float(percent_xpath[0].text)\
                or 0.0
            odoo_tax_found = False
            logger.debug(
                'select_taxes_of_invoice_line type_code=%s '
                'categ_code=%s percent=%s', type_code, categ_code, percent)
            for otax in unece2odoo_tax:
                if (
                        otax['unece_type_code'] == type_code and
                        otax['type'] == 'percent' and
                        not float_compare(
                            percent, otax['amount'], precision_digits=prec)):
                    if categ_code and categ_code != otax['unece_categ_code']:
                        continue
                    tax_ids.append(otax['id'])
                    odoo_tax_found = True
                    break
            if not odoo_tax_found:
                raise UserError(_(
                    "No tax in Odoo matched the tax "
                    "described in the XML file as Type Code = %s, "
                    "Category Code = %s and Percentage = %s "
                    "(related to: %s)") % (
                        type_code, categ_code, percent,
                        line_name or _('Global')))
        return tax_ids

    @api.model
    def parse_zugferd_xml(self, xml_root):
        """Parse Core Industry Invoice XML file"""
        namespaces = xml_root.nsmap
        prec = self.env['decimal.precision'].precision_get('Account')
        logger.debug('XML file namespaces=%s', namespaces)
        doc_type_xpath = xml_root.xpath(
            '//rsm:HeaderExchangedDocument/ram:TypeCode',
            namespaces=namespaces)
        if doc_type_xpath and doc_type_xpath[0].text != '380':
            raise UserError(_(
                "The ZUGFeRD XML file is not an invoice/refund file "
                "(TypeCode is %s") % doc_type_xpath[0].text)
        inv_number_xpath = xml_root.xpath(
            '//rsm:HeaderExchangedDocument/ram:ID', namespaces=namespaces)
        supplier_xpath = xml_root.xpath(
            '//ram:ApplicableSupplyChainTradeAgreement'
            '/ram:SellerTradeParty'
            '/ram:Name', namespaces=namespaces)
        vat_xpath = xml_root.xpath(
            '//ram:ApplicableSupplyChainTradeAgreement'
            "/ram:SellerTradeParty"
            "/ram:SpecifiedTaxRegistration"
            "/ram:ID[@schemeID='VA']",
            namespaces=namespaces)
        email_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeAgreement"
            "/ram:SellerTradeParty"
            "/ram:DefinedTradeContact"
            "/ram:EmailURIUniversalCommunication"
            "/ram:URIID", namespaces=namespaces)
        date_xpath = xml_root.xpath(
            '//rsm:HeaderExchangedDocument'
            '/ram:IssueDateTime/udt:DateTimeString', namespaces=namespaces)
        date_attrib = date_xpath[0].attrib
        if date_attrib and date_attrib.get('format') != '102':
            raise UserError(_(
                "The date format of the invoice date should be 102 "
                "in a ZUGFeRD XML file"))
        date_dt = datetime.strptime(date_xpath[0].text, '%Y%m%d')
        date_due_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradePaymentTerms"
            "/ram:DueDateDateTime"
            "/udt:DateTimeString", namespaces=namespaces)
        date_due_str = False
        if date_due_xpath:
            date_due_attrib = date_due_xpath[0].attrib
            if date_due_attrib and date_due_attrib.get('format') != '102':
                raise UserError(_(
                    "The date format of the due date should be 102 "
                    "in a ZUGFeRD XML file"))
            date_due_dt = datetime.strptime(date_due_xpath[0].text, '%Y%m%d')
            date_due_str = fields.Date.to_string(date_due_dt)
        currency_iso_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:InvoiceCurrencyCode",
            namespaces=namespaces)
        total_line_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradeSettlementMonetarySummation"
            "/ram:LineTotalAmount", namespaces=namespaces)
        total_line = total_line_xpath and float(
            total_line_xpath[0].text) or 0.0
        total_charge_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradeSettlementMonetarySummation"
            "/ram:ChargeTotalAmount", namespaces=namespaces)
        total_charge = total_charge_xpath and float(
            total_charge_xpath[0].text) or 0.0
        total_tradeallowance_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradeSettlementMonetarySummation"
            "/ram:AllowanceTotalAmount", namespaces=namespaces)
        total_tradeallowance = total_tradeallowance_xpath and float(
            total_tradeallowance_xpath[0].text) or 0
        amount_tax_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradeSettlementMonetarySummation"
            "/ram:TaxTotalAmount", namespaces=namespaces)
        amount_tax = float(amount_tax_xpath[0].text)
        amount_total_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradeSettlementMonetarySummation"
            "/ram:GrandTotalAmount",
            namespaces=namespaces)
        amount_total = float(amount_total_xpath[0].text)
        # Check coherence
        check_total = total_line + total_charge - total_tradeallowance\
            + amount_tax
        if float_compare(check_total, amount_total, precision_digits=prec):
            raise UserError(_(
                "The GrandTotalAmount is %s but the sum of "
                "the lines plus the total charge plus the total trade "
                "allowance plus the total taxes is %s.")
                % (amount_total, check_total))

        amount_untaxed = amount_total - amount_tax
        payment_type_code = xml_root.xpath(
            "//ram:SpecifiedTradeSettlementPaymentMeans"
            "/ram:TypeCode", namespaces=namespaces)
        iban_xpath = bic_xpath = False
        if payment_type_code and payment_type_code[0].text == '31':
            iban_xpath = xml_root.xpath(
                "//ram:SpecifiedTradeSettlementPaymentMeans"
                "/ram:PayeePartyCreditorFinancialAccount"
                "/ram:IBANID", namespaces=namespaces)
            bic_xpath = xml_root.xpath(
                "//ram:SpecifiedTradeSettlementPaymentMeans"
                "/ram:PayeeSpecifiedCreditorFinancialInstitution"
                "/ram:BICID", namespaces=namespaces)
        uoms = self.env['product.uom'].search([('unece_code', '!=', False)])
        unece2odoo_uom = {}
        for uom in uoms:
            unece2odoo_uom[uom.unece_code] = uom.id
        logger.debug('unece2odoo_uom = %s', unece2odoo_uom)
        taxes = self.env['account.tax'].search([
            ('unece_type_id', '!=', False),
            ('unece_categ_id', '!=', False),
            ('type_tax_use', 'in', ('all', 'purchase')),
            ('price_include', '=', False),  # TODO : check what the standard
            ])                              # says about this
        unece2odoo_tax = []
        for tax in taxes:
            unece2odoo_tax.append({
                'unece_type_code': tax.unece_type_code,
                'unece_categ_code': tax.unece_categ_code,
                'type': tax.type,
                'amount': tax.amount * 100,
                'id': tax.id,
                })
        logger.debug('unece2odoo_tax=%s', unece2odoo_tax)
        # global_tax_ids only used as fallback when taxes are not detailed
        # on invoice lines (which is the case at Basic level)
        global_taxes_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:ApplicableTradeTax", namespaces=namespaces)
        global_tax_ids = self.select_taxes_of_invoice_line(
            global_taxes_xpath, namespaces, unece2odoo_tax)
        logger.debug('global_tax_ids=%s', global_tax_ids)
        res_lines = []
        total_line_lines = 0.0
        inv_line_xpath = xml_root.xpath(
            "//ram:IncludedSupplyChainTradeLineItem", namespaces=namespaces)
        for iline in inv_line_xpath:
            price_unit_xpath = iline.xpath(
                "ram:SpecifiedSupplyChainTradeAgreement"
                "/ram:NetPriceProductTradePrice"
                "/ram:ChargeAmount",
                namespaces=namespaces)
            qty_xpath = iline.xpath(
                "ram:SpecifiedSupplyChainTradeDelivery/ram:BilledQuantity",
                namespaces=namespaces)
            if not qty_xpath:
                continue
            qty = float(qty_xpath[0].text)
            uos_id = False
            if qty_xpath[0].attrib and qty_xpath[0].attrib.get('unitCode'):
                unece_uom = qty_xpath[0].attrib['unitCode']
                uos_id = unece2odoo_uom.get(unece_uom)
            ean13_xpath = iline.xpath(
                "ram:SpecifiedTradeProduct/ram:GlobalID",
                namespaces=namespaces)
            # Check SchemeID ?
            product_code_xpath = iline.xpath(
                "ram:SpecifiedTradeProduct/ram:SellerAssignedID",
                namespaces=namespaces)
            name_xpath = iline.xpath(
                "ram:SpecifiedTradeProduct/ram:Name",
                namespaces=namespaces)
            name = name_xpath[0].text
            price_subtotal_xpath = iline.xpath(
                "ram:SpecifiedSupplyChainTradeSettlement"
                "/ram:SpecifiedTradeSettlementMonetarySummation"
                "/ram:LineTotalAmount",
                namespaces=namespaces)
            price_subtotal = float(price_subtotal_xpath[0].text)
            if price_unit_xpath:
                price_unit = float(price_unit_xpath[0].text)
            else:
                price_unit = price_subtotal / qty
            total_line_lines += price_subtotal
            # Reminder : ApplicableTradeTax not available on lines
            # at Basic level
            taxes_xpath = iline.xpath(
                "ram:SpecifiedSupplyChainTradeSettlement"
                "//ram:ApplicableTradeTax", namespaces=namespaces)
            tax_ids = self.select_taxes_of_invoice_line(
                taxes_xpath, namespaces, unece2odoo_tax, name)
            vals = {
                'product_ean13': ean13_xpath and ean13_xpath[0].text or False,
                'product_code':
                product_code_xpath and product_code_xpath[0].text or False,
                'quantity': qty,
                'uos_id': uos_id,
                'price_unit': price_unit,
                'name': name,
                'tax_ids': tax_ids or global_tax_ids,
                }
            res_lines.append(vals)

        if float_compare(
                total_line, total_line_lines, precision_digits=prec):
            logger.warning(
                "The global LineTotalAmount (%s) doesn't match the "
                "sum of the LineTotalAmount of each line (%s). It can "
                "have a diff of a few cents due to sum of rounded values vs "
                "rounded sum policies.", total_line, total_line_lines)

        charge_line_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedLogisticsServiceCharge", namespaces=namespaces)
        total_charge_lines = 0.0
        for chline in charge_line_xpath:
            name_xpath = chline.xpath(
                "ram:Description", namespaces=namespaces)
            name = name_xpath and name_xpath[0].text or _("Logistics Service")
            price_unit_xpath = chline.xpath(
                "ram:AppliedAmount", namespaces=namespaces)
            price_unit = float(price_unit_xpath[0].text)
            total_charge_lines += price_unit
            taxes_xpath = chline.xpath(
                "ram:AppliedTradeTax", namespaces=namespaces)
            tax_ids = self.select_taxes_of_invoice_line(
                taxes_xpath, namespaces, unece2odoo_tax, name)
            vals = {
                'name': name,
                'quantity': 1,
                'price_unit': price_unit,
                'tax_ids': tax_ids or global_tax_ids,
                }
            res_lines.append(vals)

        if float_compare(
                total_charge, total_charge_lines, precision_digits=prec):
            if len(global_tax_ids) <= 1 and not total_charge_lines:
                res_lines.append({
                    'name': _("Logistics Service"),
                    'quantity': 1,
                    'price_unit': total_charge,
                    'tax_ids': global_tax_ids,
                    })
            else:
                raise UserError(_(
                    "ChargeTotalAmount (%s) doesn't match the "
                    "total of the charge lines (%s). Maybe it is "
                    "because the ZUGFeRD XML file is at BASIC level, "
                    "and we don't have the details of taxes for the "
                    "charge lines.")
                    % (total_charge, total_charge_lines))

        if float_compare(total_tradeallowance, 0, precision_digits=prec) == -1:
            tradeallowance_qty = 1
        else:
            tradeallowance_qty = -1
        tradeallowance_line_xpath = xml_root.xpath(
            "//ram:ApplicableSupplyChainTradeSettlement"
            "/ram:SpecifiedTradeAllowanceCharge", namespaces=namespaces)
        total_tradeallowance_lines = 0.0
        for alline in tradeallowance_line_xpath:
            name_xpath = alline.xpath(
                "ram:Reason", namespaces=namespaces)
            name = name_xpath and name_xpath[0].text or _("Trade Allowance")
            price_unit_xpath = alline.xpath(
                "ram:ActualAmount", namespaces=namespaces)
            price_unit = abs(float(price_unit_xpath[0].text))
            total_tradeallowance_lines += price_unit
            taxes_xpath = alline.xpath(
                "ram:CategoryTradeTax", namespaces=namespaces)
            tax_ids = self.select_taxes_of_invoice_line(
                taxes_xpath, namespaces, unece2odoo_tax, name)
            vals = {
                'name': name,
                'quantity': tradeallowance_qty,
                'price_unit': price_unit,
                'tax_ids': tax_ids or global_tax_ids,
                }
            res_lines.append(vals)
        if float_compare(
                abs(total_tradeallowance), total_tradeallowance_lines,
                precision_digits=prec):
            if len(global_tax_ids) <= 1 and not total_tradeallowance_lines:
                res_lines.append({
                    'name': _("Trade Allowance"),
                    'quantity': tradeallowance_qty,
                    'price_unit': total_tradeallowance,
                    'tax_ids': global_tax_ids,
                    })
            else:
                raise UserError(_(
                    "AllowanceTotalAmount (%s) doesn't match the "
                    "total of the allowance lines (%s). Maybe it is "
                    "because the ZUGFeRD XML file is at BASIC level, "
                    "and we don't have the details of taxes for the "
                    "allowance lines.")
                    % (abs(total_tradeallowance), total_tradeallowance_lines))

        res = {
            'partner_vat': vat_xpath and vat_xpath[0].text or False,
            'partner_name': supplier_xpath[0].text,
            'partner_email': email_xpath and email_xpath[0].text or False,
            'invoice_number': inv_number_xpath[0].text,
            'date': fields.Date.to_string(date_dt),
            'date_due': date_due_str,
            'currency_iso': currency_iso_xpath[0].text,
            'amount_total': amount_total,
            'amount_untaxed': amount_untaxed,
            'iban': iban_xpath and iban_xpath[0].text or False,
            'bic': bic_xpath and bic_xpath[0].text or False,
            'lines': res_lines,
            }
        # Hack for the sample ZUGFeRD invoices that use an invalid VAT number !
        if res['partner_vat'] == 'DE123456789':
            res.pop('partner_vat')
            if not res.get('partner_email'):
                res['partner_name'] = 'Lieferant GmbH'
        logger.info('Result of ZUGFeRD XML parsing: %s', res)
        return res
