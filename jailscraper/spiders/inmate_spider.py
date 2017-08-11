import boto3
import csv
import logging
import io
import os
import requests
import scrapy

from datetime import date, datetime, timedelta
from jailscraper import app_config
from jailscraper.models import InmatePage

# Quiet down, Boto!
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logging.getLogger('s3transfer').setLevel(logging.CRITICAL)

ONE_DAY = timedelta(days=1)


class InmatesSpider(scrapy.Spider):
    name = "inmates"

    def __init__(self, category=None, testing=False, *args, **kwargs):
        super(InmatesSpider, self).__init__(*args, **kwargs)
        if app_config.USE_S3_STORAGE:
            s3 = boto3.resource('s3')
            self._bucket = s3.Bucket(app_config.S3_BUCKET)
        self._today = datetime.combine(date.today(), datetime.min.time())
        self._yesterday = self._today - ONE_DAY
        self._testing = bool(testing)

    def start_requests(self):
        for url in self._generate_urls():
            yield scrapy.Request(url=url, callback=self.parse)

    def parse(self, response):
        inmate = InmatePage(response.body)

        if app_config.USE_LOCAL_STORAGE:
            self._save_local(response, inmate)

        if app_config.USE_S3_STORAGE:
            self._save_to_s3(response, inmate)

        yield {
            'Age_At_Booking': inmate.age_at_booking,
            'Bail_Amount': inmate.bail_amount,
            'Booking_Date': inmate.booking_date,
            'Booking_Id': inmate.booking_id,
            'Charges': inmate.charges,
            'Court_Date': inmate.court_date,
            'Court_Location': inmate.court_location,
            'Gender': inmate.gender,
            'Inmate_Hash': inmate.inmate_hash,
            'Height': inmate.height,
            'Housing_Location': inmate.housing_location,
            'Race': inmate.race,
            'Weight': inmate.weight,
            'Incomplete': self._is_complete_record(inmate)
        }

    def _generate_urls(self):
        """Make URLs."""
        last_date, f = self._get_seed_file()

        last_date = datetime.strptime(last_date, '%Y-%m-%d')
        self._start_date = last_date

        reader = csv.DictReader(f)
        urls = [app_config.INMATE_URL_TEMPLATE.format(row['Booking_Id']) for row in reader]

        # If there was seed data, increment day. Otherwise, just start on fallback date
        # returned by self._get_seed_file().
        if len(urls):
            last_date = last_date + ONE_DAY

        # Scan the universe of URLs
        while last_date < self._today:
            next_query = last_date.strftime('%Y-%m%d')
            for num in range(1, app_config.MAX_DEFAULT_JAIL_NUMBER + 1):
                jailnumber = '{0}{1:03d}'.format(next_query, num)
                urls.append(app_config.INMATE_URL_TEMPLATE.format(jailnumber))
            last_date = last_date + ONE_DAY

        if self._testing:
            return urls[len(urls) - 600:]

        return urls

    def _get_seed_file(self):
        """Returns data from seed file as array of lines."""
        if app_config.USE_S3_STORAGE:
            return self._get_s3_seed_file()
        else:
            return self._get_local_seed_file()

    def _get_s3_seed_file(self):
        """Get seed file from S3. Return last date and array of lines."""

        target = app_config.TARGET
        if target:
            target = '{0}/'.format(target)

        url = "https://s3.amazonaws.com/{0}/{1}manifest.csv".format(app_config.S3_BUCKET, target)
        resp = requests.get(url)

        # For now, this "csv" is just a list of filenames, so we don't even need to parse as csv.
        seed_url = resp.text.splitlines().pop()
        last_date = seed_url.split('/')[-1].split('.')[0]
        seed_response = requests.get(seed_url)
        return last_date, io.StringIO(seed_response.text)

    def _get_local_seed_file(self):
        """Get seed file from local file system. Return array of lines."""

        try:
            files = sorted(os.listdir('data/daily'))
        except FileNotFoundError:
            files = []

        if not len(files):
            self.log('No seed file found.')
            return app_config.FALLBACK_START_DATE, []

        last_file = os.path.join('data/daily', files[-1])
        last_date = files[-1].split('.')[0]
        f = open(last_file)
        self.log('Used {0} from local file system to seed scrape.'.format(last_file))
        return last_date, f

    def _save_local(self, response, inmate):
        """Save scraped page to local filesystem."""
        os.makedirs('data/raw', exist_ok=True)
        filepath = os.path.join('data/raw', self._generate_page_filename(inmate))
        with open(filepath, 'wb') as f:
            f.write(response.body)
        self.log('Wrote {0} to local file system'.format(filepath))

    def _save_to_s3(self, response, inmate):
        """Save scraped page to s3."""
        key = '{0}/raw/{1}'.format(app_config.TARGET, self._generate_page_filename(inmate))
        if key.startswith('/'):
            key = key[1:]
        f = io.BytesIO(response.body)
        self._bucket.upload_fileobj(f, key)
        self.log('Uploaded s3://{0}/{1}'.format(app_config.S3_BUCKET, key))

    def _generate_page_filename(self, inmate):
        """Make a scraped page filename."""
        name = '{0}-{1}.html'.format(self._today.strftime('%Y-%m-%d'), inmate.booking_id)
        return name

    def _is_complete_record(self, inmate):
        """Was this scrape run daily?"""
        booking_date = datetime.strptime(inmate.booking_date, '%Y-%m-%d')
        return booking_date < self._yesterday
