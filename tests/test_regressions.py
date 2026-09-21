"""Offline regression tests for recommendation, execution and data integrity."""

import copy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from analysis_engine import AnalysisEngine
from data_acquisition import DataAcquisition
from data_quality import latest_completed_session, extract_report_date, recommendation_expiry
from dividend_calendar import DividendCalendar, apply_dividend_calendar
from fundamental_analysis import FundamentalAnalysis
from market_context import compute_sector_medians
from price_validation import PriceValidator, apply_official_close
from recommender import build_candidate_list
from scoring import score_stock, _score_quality, _score_momentum, _score_dividend, _score_liquidity
from signal_history import write_daily_snapshot, load_all_snapshots, write_local_snapshot, load_local_snapshots
from track_record import compute_track_record
from portfolio.allocation import allocate_budget


def prices(values, end='2026-09-21'):
    close = np.array(values, dtype=float)
    frame = pd.DataFrame({'open': close, 'close': close, 'high': close + .1,
                          'low': close - .1, 'volume': 500_000},
                         index=pd.bdate_range(end=end, periods=len(close)))
    frame.attrs.update(source='TradingView', identity_verified=True)
    return frame


def eligible(symbol='AAA', score=75, rating=.2):
    result = AnalysisEngine().analyze_stock(prices(np.linspace(80, 100, 80)))
    fund = {'sector':'Utilities', 'tech_rating':rating}
    scores = {'overall':score, 'coverage':100, 'value':75, 'quality':75, 'growth':75}
    validation = {'status':'ok', 'is_stale':False}
    return {symbol: result}, {symbol: fund}, {symbol: scores}, {symbol: validation}


class Indicators(unittest.TestCase):
    def setUp(self):
        self.engine = AnalysisEngine()

    def test_rsi_rising_falling_and_flat(self):
        for sequence, expected in [(range(1,61),100), (range(60,0,-1),0), ([100]*60,50)]:
            with self.subTest(expected=expected):
                self.assertEqual(self.engine.calculate_rsi(pd.Series(sequence)).iloc[-1], expected)

    def test_wilder_initial_mean(self):
        # Changes +1, -1, +2: seed gain=1, loss=1/3 => RSI=75.
        rsi = self.engine.calculate_rsi(pd.Series([10.,11.,10.,12.,11.]), window=3)
        self.assertAlmostEqual(rsi.iloc[3],75)
        self.assertAlmostEqual(rsi.iloc[4], 100 - 100/(1 + (2/3)/(5/9)))

    def test_flat_has_no_crossovers(self):
        result = self.engine.analyze_stock(prices([100]*80))
        self.assertEqual(result['signals']['macd'], 'neutral')
        self.assertEqual(result['signals']['ma_crossover'], 'neutral')
        self.assertEqual(result['signals']['overall'], 'neutral')

    def test_short_history_not_a_signal(self):
        result = self.engine.analyze_stock(prices([100]))
        self.assertFalse(result['history_complete'])
        self.assertEqual(result['signals']['overall'], 'undefined')
        self.assertEqual(result['signals']['macd'], 'undefined')

    def test_invalid_and_duplicate_history_rejected(self):
        for value in [0,-1,float('nan'),float('inf')]:
            self.assertEqual(self.engine.analyze_stock(prices([100,value])), {})
        data = prices([100,101])
        data.index = [data.index[0],data.index[0]]
        self.assertEqual(self.engine.analyze_stock(data), {})

    def test_technical_rating_boundaries(self):
        for rating, label in [(.1,'neutral'),(.5,'buy'),(-.1,'neutral'),(-.5,'sell'),
                               (.51,'strong_buy'),(-.51,'strong_sell'),(float('nan'),'undefined'),(2,'undefined')]:
            self.assertEqual(FundamentalAnalysis.signal_from_tech_rating(rating)[1],label)


class CandidateRules(unittest.TestCase):
    def screen(self, result, fund, scores, validations):
        return build_candidate_list(result,fund,scores,validations=validations,as_of='2026-09-21')

    def test_valid_candidate(self):
        self.assertEqual(len(self.screen(*eligible())),1)

    def test_missing_inputs_fail_closed(self):
        base = eligible()
        cases = [(0,'identity_verified',False),(0,'history_complete',False),
                 (0,'history_date','2026-09-18'),(0,'median_value_traded_20d',None),
                 (0,'median_value_traded_20d',0),(2,'overall',None),(2,'coverage',None),
                 (2,'overall',59),(2,'coverage',79),(2,'quality',None),
                 (3,'status','mismatch'),(3,'status','stale'),(3,'status','unverified')]
        for idx,key,value in cases:
            args=copy.deepcopy(base)
            args[idx]['AAA'][key]=value
            with self.subTest(key=key,value=value):
                self.assertEqual(self.screen(*args),[])
        self.assertEqual(build_candidate_list(*base[:3],as_of='2026-09-21'),[])

    def test_nonfinite_scores_and_prices(self):
        for value in [float('nan'),float('inf'),-1,0]:
            args=eligible()
            args[0]['AAA']['latest']['close']=value
            self.assertEqual(self.screen(*args),[])
        args=eligible()
        args[2]['AAA']['overall']=float('nan')
        self.assertEqual(self.screen(*args),[])

    def test_score_precedes_technical_tier(self):
        a=eligible('AAA',60,.8)
        b=eligible('BBB',95,.2)
        args=[{**x,**y} for x,y in zip(a,b)]
        self.assertEqual([c['symbol'] for c in self.screen(*args)],['BBB','AAA'])

    def test_exclusion_explanations(self):
        reasons={}
        build_candidate_list({'X':{}},{},{},as_of='2026-09-21',exclusions=reasons)
        self.assertIn('invalid price',reasons['X'])


class ScoringRules(unittest.TestCase):
    def test_singleton_uses_absolute_valuation(self):
        scores=[]
        for pe in [5,50]:
            fund={'X':{'sector':'Solo','pe_ratio':pe}}
            scores.append(score_stock('X',{},fund['X'],sector_medians=compute_sector_medians(fund))['value'])
        self.assertGreater(scores[0],scores[1])

    def test_peer_count_is_metric_specific(self):
        fund={'A':{'sector':'S','pe_ratio':5},'B':{'sector':'S'},'C':{'sector':'S'}}
        self.assertEqual(compute_sector_medians(fund)['S']['pe_ratio_count'],1)

    def test_financial_quality_ignores_industrial_ratios(self):
        base={'sector':'Finance','roe':15,'roa':2}
        a=_score_quality({**base,'debt_to_equity':.1,'current_ratio':2})[0]
        b=_score_quality({**base,'debt_to_equity':100,'current_ratio':.01})[0]
        self.assertEqual(a,b)

    def test_dividend_unknown_or_uncovered_not_rewarded(self):
        self.assertIsNone(_score_dividend({'dividend_yield':10})[0])
        for payout in [-10,0,120]:
            self.assertEqual(_score_dividend({'dividend_yield':10,'dividend_payout_ratio':payout})[0],0)
        self.assertEqual(_score_dividend({'dividend_yield':0})[0],0)

    def test_liquidity_is_sustained_and_zero_is_not_missing(self):
        self.assertEqual(_score_liquidity({'median_value_traded_20d':0})[0],0)
        self.assertIsNone(_score_liquidity({'value_traded':100_000_000})[0])

    def test_rsi_scoring_has_no_threshold_jumps(self):
        for boundary in [30,70]:
            a=_score_momentum({'latest':{'rsi':boundary-.01}},{})[0]
            b=_score_momentum({'latest':{'rsi':boundary+.01}},{})[0]
            self.assertLessEqual(abs(a-b),1)


class PriceAndDividendData(unittest.TestCase):
    def test_reference_parser_keeps_explicit_board_date(self):
        html = ('<h1>September 21, 2026</h1><table><tr><td><a href="/nse/aaa/">AAA</a>'
                '<td><a>Example</a><td>1,000<td>100.00<td>+1.00')
        record = PriceValidator._parse_afx(html)['AAA']
        self.assertEqual(record['date'], '2026-09-21')
        self.assertEqual(record['price'], 100)

    def test_missing_sessions_do_not_prove_sustained_liquidity(self):
        frame = prices([100]*80).drop(prices([100]*80).index[-10])
        result = AnalysisEngine().analyze_stock(frame)
        self.assertIsNone(result['median_value_traded_20d'])

    def test_weekend_and_public_holiday_sessions(self):
        self.assertEqual(str(latest_completed_session(datetime(2026,9,21,10))), '2026-09-18')
        self.assertEqual(str(latest_completed_session(datetime(2026,6,1,17))), '2026-05-29')
        self.assertEqual(recommendation_expiry('2026-05-29'),'2026-06-02T15:30:00+03:00')

    def test_explicit_dates_only(self):
        for text in ['2026-07-30','30-JUL-26','Thursday, July 30, 2026','30 July 2026']:
            self.assertEqual(str(extract_report_date(text)),'2026-07-30')
        self.assertIsNone(extract_report_date('No trading date'))

    def test_validation_requires_matching_date_and_real_volume(self):
        pv=PriceValidator.__new__(PriceValidator)
        pv.disagree_threshold_pct=1
        pv._reference={'AAA':{'price':100,'date':'2026-09-21'}}
        data=prices([100]*60)
        self.assertEqual(pv.validate('AAA',100,data,as_of='2026-09-21')['status'],'ok')
        data.iloc[-1,data.columns.get_loc('volume')]=0
        self.assertEqual(pv.validate('AAA',100,data,as_of='2026-09-21')['status'],'stale')
        pv._reference['AAA'].pop('date')
        self.assertEqual(pv.validate('AAA',100,data,as_of='2026-09-21')['status'],'unverified')

    def test_crosscheck_cannot_change_indicator_inputs(self):
        result=AnalysisEngine().analyze_stock(prices(np.linspace(80,100,80)))
        before=copy.deepcopy(result)
        apply_official_close({'X':result},{'X':{'price':50,'date':'2026-09-21'}})
        self.assertEqual(result['latest']['close'],before['latest']['close'])
        self.assertEqual(result['signals'],before['signals'])
        pd.testing.assert_frame_equal(result['data'],before['data'])

    def test_declaration_does_not_replace_annual_metrics_or_ex_date(self):
        html='<p>Mar 10 2026 AAA Example Company: Payment of KES 2 interim dividend</p><p>Aug 10 2026 AAA Example Company: Payment of KES 3 final dividend</p>'
        fund={'AAA':{'close':100,'dps_fy':5,'dividend_yield':5,'dividend_ex_date':'2026-07-01'}}
        with patch.object(DividendCalendar,'fetch',return_value=DividendCalendar._parse(html)):
            with tempfile.TemporaryDirectory() as folder:
                apply_dividend_calendar(fund,folder)
        self.assertEqual(fund['AAA']['dps_fy'],5)
        self.assertEqual(fund['AAA']['dividend_yield'],5)
        self.assertEqual(fund['AAA']['declared_dividend'],3)
        self.assertEqual(fund['AAA']['dividend_ex_date'],'2026-07-01')

    def test_yahoo_never_accepts_wrong_exchange_or_tries_bare_ticker(self):
        da=DataAcquisition.__new__(DataAcquisition)
        with patch('yfinance.Ticker') as ticker, patch('yfinance.download') as download:
            ticker.return_value.get_info.return_value={'currency':'USD','exchange':'NYQ'}
            self.assertIsNone(da._fetch_from_yahoo('ABC'))
            download.assert_not_called()
            ticker.assert_called_once_with('ABC.NR')

    def test_quote_fallback_does_not_short_circuit_history(self):
        da=DataAcquisition.__new__(DataAcquisition)
        da.data_sources=['nse_pdf','yahoo_finance']
        historical=prices([100]*60)
        with patch.object(da,'_fetch_from_source',side_effect=[prices([100]),historical]), patch.object(da,'_save_to_cache'):
            self.assertIs(da.fetch_stock_data('AAA',force_refresh=True),historical)

    def test_pdf_without_report_date_rejected(self):
        da=DataAcquisition.__new__(DataAcquisition)
        da._pdf_cache={'AAA':{'open':1,'high':1,'low':1,'close':1,'volume':100}}
        da._pdf_cache_date=datetime.now().date()
        self.assertIsNone(da._fetch_from_nse_pdf('AAA'))


class PortfolioRules(unittest.TestCase):
    def test_fees_fit_budget(self):
        items,cash=allocate_budget([{'symbol':'AAA','price':100}],10000,
                                   max_stock_weight=1,max_sector_weight=1)
        self.assertEqual(items[0]['shares'],98)
        self.assertEqual(items[0]['cash_required_kes'],9947)
        self.assertEqual(cash,53)

    def test_affordable_shares_not_all_discarded(self):
        items,cash=allocate_budget([{'symbol':'A','price':60},{'symbol':'B','price':70}],100,
                                   fee_pct=0,max_stock_weight=1,max_sector_weight=1)
        self.assertEqual(items[0]['symbol'],'A')
        self.assertEqual(cash,40)

    def test_position_and_sector_caps_include_holdings(self):
        candidates=[{'symbol':s,'price':10,'sector':'Finance'} for s in ['A','B','C','D','E']]
        holdings={'A':{'qty':1000,'market_value':10000,'sector':'Finance'}}
        items,cash=allocate_budget(candidates,10000,holdings=holdings)
        self.assertEqual(items,[])
        self.assertEqual(cash,10000)
        items,cash=allocate_budget(candidates,10000)
        self.assertLessEqual(sum(a['allocated_kes'] for a in items),4000)
        self.assertTrue(all(a['allocated_kes'] <= 2000 for a in items))

    def test_unpriced_holdings_and_nonfinite_budget_rejected(self):
        with self.assertRaises(ValueError):
            allocate_budget([],10000,holdings={'A':{'qty':1,'market_value':None}})
        for value in [float('nan'),float('inf'),-1,0]:
            with self.assertRaises(ValueError):
                allocate_budget([],value)


def snapshots():
    result={}
    for day in pd.bdate_range('2026-09-01','2026-09-30'):
        d=str(day.date())
        result[d]={'AAA':{'price':100.,'price_date':d,'price_verified':True,
                         'schema_version':2,'selected':True,'allocation_shares':100,
                         'model_budget_kes':100_000,'fee_pct':.015,'investable':True,
                         'tv_class':'strong_buy'}}
    return result


class EvaluationRules(unittest.TestCase):
    def test_local_snapshot_is_immutable_and_legacy_cannot_override_it(self):
        result, fund, scores, validations = eligible()
        kwargs = dict(date='2026-09-21', validations=validations)
        with tempfile.TemporaryDirectory() as folder:
            path = write_local_snapshot(folder, result, fund, scores, **kwargs)
            before = path.read_bytes()
            result['AAA']['latest']['close'] = 999
            write_local_snapshot(folder, result, fund, scores, **kwargs)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(load_local_snapshots(folder)['2026-09-21']['AAA']['price'], 100)

    def test_wrong_date_quote_is_not_an_execution(self):
        data = snapshots()
        data['2026-09-02']['AAA']['price_date'] = '2026-09-09'
        result = compute_track_record(data, horizon_days=5)
        self.assertEqual(result['portfolio']['incomplete_periods'], 1)

    def test_report_displays_model_and_limitations(self):
        from report_generator import ReportGenerator
        generator = ReportGenerator.__new__(ReportGenerator)
        result = compute_track_record(snapshots(), horizon_days=5)
        html = generator._build_track_record_body(track_record_new={5: result})
        self.assertIn('Model portfolio (after assumed costs)', html)
        self.assertIn('excluding dividends and corporate actions', html)

    def test_legacy_and_unselected_not_reported_as_portfolio(self):
        data=snapshots()
        for rows in data.values():
            rows['AAA']['selected']=False
        self.assertEqual(compute_track_record(data)['portfolio']['n'],0)
        for rows in data.values():
            rows['AAA'].pop('schema_version')
        self.assertEqual(compute_track_record(data)['portfolio']['n'],0)

    def test_next_session_entry_costs_and_nonoverlap(self):
        data=snapshots()
        # A same-day-to-next-day jump is not available to an after-close call.
        data['2026-09-01']['AAA']['price']=10
        result=compute_track_record(data,horizon_days=5,slippage_pct=0)
        periods=result['portfolio']['periods']
        self.assertTrue(periods)
        self.assertEqual(periods[0]['entry_date'],'2026-09-02')
        self.assertAlmostEqual(periods[0]['return_pct'],-.3)
        for a,b in zip(periods,periods[1:]):
            self.assertGreaterEqual(b['signal_date'],a['exit_date'])
        self.assertIn('dividends and corporate actions',result['note'])

    def test_missing_exit_invalidates_entire_period(self):
        data=snapshots()
        data['2026-09-09']={}
        result=compute_track_record(data,horizon_days=5)
        self.assertEqual(result['portfolio']['incomplete_periods'],1)
        self.assertNotIn('2026-09-01',[p['signal_date'] for p in result['portfolio']['periods']])

    def test_snapshot_stores_actual_selected_allocation(self):
        result,fund,scores,validations=eligible()
        candidates=build_candidate_list(result,fund,scores,validations=validations,as_of='2026-09-21')
        s3=Mock()
        self.assertEqual(write_daily_snapshot(s3,'bucket',result,fund,scores,date='2026-09-21',
                         candidates=candidates,validations=validations),1)
        kwargs=s3.put_object.call_args.kwargs
        self.assertEqual(kwargs['IfNoneMatch'],'*')
        payload=json.loads(kwargs['Body'])
        self.assertTrue(payload['symbols']['AAA']['selected'])
        self.assertGreater(payload['symbols']['AAA']['allocation_shares'],0)


if __name__ == '__main__':
    unittest.main()
