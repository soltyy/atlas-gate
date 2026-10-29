"""Generate reviewed UTC intervals; stdout contains public prices, never credentials.

Calendar dates here are explicit UTC off-peak dates attested by the operator.
No network scraping or implicit recurring refresh of a signed profile.
"""
from datetime import datetime, timedelta, timezone, date
import argparse
import json

from atlas_gate.pricing import Schedule

RATES = {'deepseek-flash': (.15, .003, .6), 'deepseek-v4-pro': (.66, .022, 1.98)}


def generate(start, end, checked, holidays, calendar_source, revision):
    if start.time() != datetime.min.time() or end.time() != datetime.min.time():
        raise ValueError('calendar horizon must begin/end at UTC midnight')
    if end <= start or (end-start).days > 366:
        raise ValueError('explicit calendar horizon must be 1..366 days')
    result=[]
    for model, rates in RATES.items():
        periods=[]; day=start
        while day < end:
            for a,b in [(0,1),(1,4),(4,6),(6,10),(10,24)]:
                peak = day.weekday() < 5 and day.date() not in holidays and a in (1,6)
                factor = 2 if peak else 1
                p={'starts_at':day+timedelta(hours=a),'ends_at':day+timedelta(hours=b),
                   'tariff_id':'peak' if peak else 'offpeak',
                   'rates':dict(zip(('input_uncached','input_cached','output'),[r*factor for r in rates]))}
                if periods and periods[-1]['tariff_id']==p['tariff_id']:
                    periods[-1]['ends_at']=p['ends_at']
                else:periods.append(p)
            day+=timedelta(days=1)
        result.append(Schedule(route_id='deepseek-api',model=model,pricing_version=revision,
            source_url='https://api-docs.deepseek.com/quick_start/pricing/',calendar_source_url=calendar_source,
            checked_at=checked,valid_from=start,valid_until=end,periods=periods,
            excluded_charges=['search','fetch','other']))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--from-date',required=True);p.add_argument('--until-date',required=True)
    p.add_argument('--checked-at',required=True);p.add_argument('--calendar-source',required=True)
    p.add_argument('--revision',required=True)
    p.add_argument('--offpeak-utc-date',action='append',default=[])
    p.add_argument('--calendar-reviewed',action='store_true',required=True)
    args=p.parse_args()
    midnight=lambda s: datetime.combine(date.fromisoformat(s),datetime.min.time(),timezone.utc)
    docs=generate(midnight(args.from_date),midnight(args.until_date),datetime.fromisoformat(args.checked_at),
                  {date.fromisoformat(d) for d in args.offpeak_utc_date},args.calendar_source,args.revision)
    print(json.dumps([doc.model_dump(mode='json') for doc in docs],ensure_ascii=False,indent=2))
