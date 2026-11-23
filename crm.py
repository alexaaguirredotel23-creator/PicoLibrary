"""
# crm.py
# Conference CRM records, Oracle REST client, model, and navigation controller.
# Author: Codex
"""
import gc
import time
from Log import *


def ticks():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.monotonic() * 1000)


def elapsed(start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(ticks(), start)
    return ticks() - start


def key(text):
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def field(row, *names):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def person_name(row):
    first = field(row, "firstname", "contactfirstname", "custfirstname", "givenname", "fname", "contactfname")
    last = field(row, "lastname", "contactlastname", "custlastname", "surname", "lname", "contactlname")
    return (first + " " + last).strip()


def required(value, row, description):
    if not value:
        raise ValueError("Missing %s. Columns: %s" % (description, ", ".join(row.keys())))
    return value


class Customer:
    """A customer list entry with details attached when they are loaded."""

    def __init__(self, row):
        self.id = required(field(row, "customerid", "custid", "clientid", "id"), row, "customer ID")
        name = field(row, "customername", "custname", "clientname", "name",
                     "customer", "companyname", "company", "organization", "custcompany")
        self.name = required(name or person_name(row), row, "customer name")
        self.contacts = None
        self.activities = None


class Contact:
    """The name and business role of one person at the selected customer."""

    def __init__(self, row):
        name = field(row, "contactname", "fullname", "name", "contact")
        self.name = required(name or person_name(row), row, "contact name")
        self.role = field(row, "role", "contactrole", "jobtitle", "contacttitle", "title", "position", "contacttype")

    def label(self):
        """Return the text used on the contact screen."""
        return self.name + (" / " + self.role if self.role else "")


class Activity:
    """An interaction recorded for a customer, with its date if supplied."""

    def __init__(self, row):
        title = field(row, "subject", "summary", "activityname", "activitysummary", "activitysubject",
                      "activitytitle", "title", "activity", "activitydescription", "description",
                      "activitytype", "type", "notes", "activitynote", "activitynotes")
        self.title = required(title, row, "activity title")
        self.date = field(row, "activitydate", "date", "performedon", "startdate", "createdon")

    def label(self):
        """Display the interaction and, when available, its calendar date."""
        return self.title + (" / " + self.date[:10] if self.date else "")


class CRMService:
    """Read paginated customer data from the course ORDS service.

    Parameters:
    ----------
    base : str
        Course REST root ending in /customer.

    Examples:
    ---------
    service.customers() returns Customer objects from all pages.
    """

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.requests = 0

    def _get(self, url):
        try:
            import requests
        except ImportError:
            import urequests as requests
        self.requests += 1
        Log.i("REST GET " + url[len(self.base):])
        response = requests.get(url, timeout=20)
        try:
            if response.status_code != 200:
                raise OSError("HTTP %s" % response.status_code)
            return response.json()
        finally:
            response.close()

    def _records(self, path, collection, record_type):
        url = self.base + "/" + path
        seen = set()
        records = []
        while url:
            if url in seen:
                raise ValueError("Repeated next page for " + path)
            seen.add(url)
            page = self._get(url)
            if isinstance(page, list):
                rows = page
                next_url = None
            elif isinstance(page, dict):
                rows = page.get("items", page.get(collection))
                if not isinstance(rows, list):
                    raise ValueError("Missing row list for " + path + ". Keys: " + str(list(page)))
                next_url = None
                for link in page.get("links", []):
                    if link.get("rel") == "next":
                        next_url = link.get("href")
                        break
                if next_url:
                    if next_url.startswith("?"):
                        next_url = url.split("?")[0] + next_url
                    if next_url.startswith("/"):
                        next_url = "/".join(self.base.split("/")[:3]) + next_url
                    if not next_url.startswith(self.base + "/"):
                        raise ValueError("Unexpected next page URL: " + str(next_url))
                elif page.get("hasMore"):
                    offset = int(page.get("offset", 0)) + len(rows)
                    if not rows:
                        raise ValueError("Empty page with hasMore for " + path)
                    next_url = self.base + "/" + path + "?offset=" + str(offset)
            else:
                raise ValueError("Expected a JSON object or array for " + path)
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Expected a record object in " + path)
                records.append(record_type({key(k): v for k, v in row.items()}))
            url = next_url
            del page, rows
            gc.collect()
        Log.i("CRMService returned %d %s" % (len(records), collection))
        return records

    def customers(self):
        """Read customer IDs and names."""
        return self._records("get_customers", "customers", Customer)

    def contacts(self, customer_id):
        """Read contacts for one customer ID."""
        return self._records("get_contacts/" + str(customer_id), "contacts", Contact)

    def activities(self, customer_id):
        """Read activities for one customer ID."""
        return self._records("get_activities/" + str(customer_id), "activities", Activity)


class CRMModel:
    """Store the directory and apply either the A or B retrieval strategy."""

    def __init__(self, service, mode):
        if mode not in ("A", "B"):
            raise ValueError("Choose A or B")
        self.service = service
        self.mode = mode
        self.customers = []

    def _load_details(self, customer):
        Log.i("CRMModel -> CRMService.contacts(%s)" % customer.id)
        contacts = self.service.contacts(customer.id)
        Log.i("CRMModel -> CRMService.activities(%s)" % customer.id)
        activities = self.service.activities(customer.id)
        # Only publish a complete pair, even if the second request fails.
        customer.contacts, customer.activities = contacts, activities

    def load(self):
        """Fetch the directory and, in A, all customer details before browsing."""
        Log.i("CRMModel -> CRMService.customers()")
        directory = self.service.customers()
        if self.mode == "A":
            for customer in directory:
                self._load_details(customer)
        self.customers = directory
        return directory

    def select(self, index):
        """Fetch on selection in B. In A the selected records are in memory."""
        customer = self.customers[index]
        if self.mode == "B":
            self._load_details(customer)
        else:
            Log.i("CRMModel: read preloaded details for " + customer.name)
        return customer

    def leave(self, index):
        """Release the selected customer's details in B when returning to the list."""
        if self.mode == "B":
            customer = self.customers[index]
            customer.contacts = customer.activities = None
            gc.collect()


class CRMController:
    """Map Next, Open/Tab and Back buttons to the directory and detail screens."""

    def __init__(self, model, view, preferred_name=""):
        self.model = model
        self.view = view
        self.preferred_name = preferred_name.lower()
        self.screen = "directory"
        self.index = self.detail = 0

    def start(self):
        """Load the directory and show the preferred customer when present."""
        start = ticks()
        calls = self.model.service.requests
        Log.i("CRMController -> CRMModel.load() mode=" + self.model.mode)
        directory = self.model.load()
        for i, customer in enumerate(directory):
            if customer.name.lower() == self.preferred_name:
                self.index = i
                break
        self.render()
        Log.i("TIMING directory ready: %d ms, %d GETs" %
              (elapsed(start), self.model.service.requests - calls))

    def handle(self, button):
        """Advance a record, open a customer, switch detail type, or go back."""
        if not self.model.customers:
            return
        Log.i("CRMController.handle(" + button + ") from button input")
        customer = self.model.customers[self.index]
        if self.screen == "directory":
            if button == "open":
                start = ticks()
                calls = self.model.service.requests
                self.view.show("Opening customer", customer.name)
                Log.i("CRMController -> CRMModel.select(%d) id=%s" % (self.index, customer.id))
                customer = self.model.select(self.index)
                self.screen, self.detail = "contacts", 0
                self.view.signal(len(customer.activities))
                self.render()
                Log.i("TIMING selection ready: %d ms, %d GETs" %
                      (elapsed(start), self.model.service.requests - calls))
                return
            step = -1 if button == "back" else 1
            self.index = (self.index + step) % len(self.model.customers)
        elif button == "back":
            Log.i("CRMController -> CRMModel.leave(%d)" % self.index)
            self.model.leave(self.index)
            self.view.neutral()
            self.screen = "directory"
        elif button == "open":
            self.screen = "activities" if self.screen == "contacts" else "contacts"
            self.detail = 0
        else:
            count = len(customer.contacts if self.screen == "contacts" else customer.activities)
            self.detail = (self.detail + 1) % max(1, count)
        self.render()

    def render(self):
        """Show a client, a contact, an activity, or an explicit empty state."""
        if not self.model.customers:
            self.view.show("Customers", "No customers")
            return
        customer = self.model.customers[self.index]
        if self.screen == "directory":
            self.view.show("Client %d/%d" % (self.index + 1, len(self.model.customers)), customer.name)
            return
        contacts = self.screen == "contacts"
        rows = customer.contacts if contacts else customer.activities
        heading = "C" if contacts else "A"
        text = "%s%d %s" % (heading, self.detail + 1, rows[self.detail].label()) if rows else "No " + self.screen
        self.view.show(customer.name, text)


if __name__ == "__main__":
    person = Contact({"firstname": "Lina", "lastname": "Cruz", "role": "Host"})
    assert person.label() == "Lina Cruz / Host"
    event = Activity({"subject": "Check-in", "activitydate": "2026-11-20"})
    assert event.label() == "Check-in / 2026-11-20"
    customer = Customer({"id": 1, "name": "Demo"})
    assert customer.id == "1" and customer.activities is None
    Log.i("Testing complete - CRM records passed")
