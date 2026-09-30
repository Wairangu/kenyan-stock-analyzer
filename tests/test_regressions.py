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
from analysis_engine import AnalysisEngine, MOMENTUM_LOOKBACK_BARS, MOMENTUM_SKIP_BARS
from data_acquisition import DataAcquisition
from data_quality import latest_completed_session, extract_report_date, recommendation_expiry
from dividend_calendar import DividendCalendar, apply_dividend_calendar
from fundamental_analysis import FundamentalAnalysis
from market_context import compute_sector_medians
from price_validation import PriceValidator, apply_official_close
from recommender import (build_candidate_list, round_trip_breakeven_pct,
                         DEFAULT_HOLD_SCORE, DEFAULT_MIN_SCORE)
from scoring import (score_stock, score_universe, build_cross_section, MIN_CROSS_SECTION,
                     _score_quality, _score_momentum, _score_dividend, _score_liquidity)
from signal_history import write_daily_snapshot, load_all_snapshots, write_local_snapshot, load_local_snapshots
from track_record import compute_track_record, DEFAULT_HORIZON_DAYS
from information_coefficient import (compute_ic, compute_ic_decay, spearman,
                                     snapshots_from_archive, MIN_NAMES,
                                     MAX_DROPPED_FRACTION)
from data_quality import next_session
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

    def test_rsi_no_longer_drives_momentum(self):
        # RSI is a mean-reversion oscillator. Scoring it linearly rewarded the
        # overbought names generate_alerts warns about, so screen-v3 removed it
        # from the factor and kept it as an alert only.
        base={'signals':{'overall':'bullish'},'momentum_12_1':10}
        scores={_score_momentum({**base,'latest':{'rsi':rsi}},{})[0] for rsi in (5,50,95)}
        self.assertEqual(len(scores),1)
        self.assertIsNone(_score_momentum({'latest':{'rsi':95}},{})[0])

    def test_momentum_ignores_three_month_return(self):
        # Short-horizon returns reverse; perf_3m is reported, never scored.
        a=_score_momentum({'signals':{'overall':'neutral'}},{'perf_3m':-40})[0]
        b=_score_momentum({'signals':{'overall':'neutral'}},{'perf_3m':40})[0]
        self.assertEqual(a,b)


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


class CrossSectionalScoring(unittest.TestCase):
    """screen-v3 ranks metrics against the day's universe instead of anchors."""

    def universe(self, funds):
        return score_universe({s: {'latest': {}} for s in funds}, funds,
                              sector_medians=compute_sector_medians(funds))

    def test_ranks_discriminate_where_absolute_anchors_pin(self):
        # Most NSE banks trade under 1x book, which the old anchor mapped to
        # 100 for every one of them -- no discrimination exactly where the
        # universe lives.
        funds = {f'B{i}': {'sector': 'Finance', 'price_to_book': pb}
                 for i, pb in enumerate([.2,.3,.4,.5,.6,.7,.8,.9,.95,.99])}
        anchored = {score_stock(s, {}, f)['value'] for s, f in funds.items()}
        self.assertEqual(anchored, {100})
        ranked = self.universe(funds)
        self.assertEqual(len({v['value'] for v in ranked.values()}), len(funds))
        self.assertGreater(ranked['B0']['value'], ranked['B9']['value'])
        self.assertTrue(ranked['B0']['ranked'])

    def test_small_universe_falls_back_to_anchors(self):
        funds = {f'S{i}': {'sector': 'X', 'price_to_book': .2 + i/10}
                 for i in range(MIN_CROSS_SECTION - 1)}
        ranked = self.universe(funds)
        self.assertEqual({v['value'] for v in ranked.values()}, {100})
        self.assertFalse(ranked['S0']['ranked'])

    def test_valuation_rank_is_relative_to_the_stock_sector(self):
        # Cheap for a bank beats cheap for a manufacturer even at a higher
        # absolute P/E, because each is ranked against its own peers.
        funds = {}
        for i, pe in enumerate([30, 40, 50, 60]):
            funds[f'BANK{i}'] = {'sector': 'Finance', 'pe_ratio': pe}
        for i, pe in enumerate([4, 5, 6, 7]):
            funds[f'IND{i}'] = {'sector': 'Industrial', 'pe_ratio': pe}
        ranked = self.universe(funds)
        self.assertTrue(ranked['BANK0']['ranked'])
        self.assertGreater(ranked['BANK0']['value'], ranked['IND3']['value'])
        self.assertLess(funds['IND3']['pe_ratio'], funds['BANK0']['pe_ratio'])

    def test_identical_metrics_share_a_rank(self):
        funds = {f'T{i}': {'sector': 'X', 'roe': 10} for i in range(MIN_CROSS_SECTION)}
        self.assertEqual(len({v['quality'] for v in self.universe(funds).values()}), 1)

    def test_single_outlier_does_not_compress_the_rest(self):
        # The point of percentile ranks over z-scores on skewed NSE metrics: a
        # 1000x outlier would pull the mean and inflate the deviation, leaving
        # everyone else bunched together. Ranks keep the spread and the order.
        base = {f'N{i}': {'sector': 'X', 'roe': float(i)} for i in range(MIN_CROSS_SECTION)}
        after = self.universe({**base, 'OUT': {'sector': 'X', 'roe': 1e6}})
        ordinary = [after[f'N{i}']['quality'] for i in range(MIN_CROSS_SECTION)]
        self.assertEqual(ordinary, sorted(ordinary))
        self.assertGreater(max(ordinary) - min(ordinary), 50)
        self.assertGreater(after['OUT']['quality'], max(ordinary))

    def test_rank_uses_the_same_day_universe_only(self):
        funds = {f'C{i}': {'sector': 'X', 'roe': float(i)} for i in range(MIN_CROSS_SECTION)}
        ctx = build_cross_section({s: dict(f) for s, f in funds.items()})
        self.assertEqual(ctx.size('roe'), MIN_CROSS_SECTION)
        self.assertIsNone(ctx.rank('roe', 'NOT_TODAY'))

    def test_metric_counts_expose_thin_factors(self):
        thin = score_stock('X', {}, {'sector': 'X', 'current_ratio': 2})
        self.assertEqual(thin['metric_counts'].get('quality'), 1)
        self.assertEqual(thin['coverage'], 20)


class MomentumFactor(unittest.TestCase):
    def setUp(self):
        self.engine = AnalysisEngine()

    def test_12_1_window_skips_the_latest_month(self):
        # Twelve months up, then a crash inside the skipped month: the factor
        # must still read positive, which is the whole point of the skip.
        close = pd.Series(list(np.linspace(100, 200, MOMENTUM_LOOKBACK_BARS + 1))
                          + list(np.linspace(200, 120, MOMENTUM_SKIP_BARS)))
        value = self.engine.calculate_momentum_12_1(close)
        start = close.iloc[-(MOMENTUM_LOOKBACK_BARS + 1)]
        end = close.iloc[-(MOMENTUM_SKIP_BARS + 1)]
        self.assertAlmostEqual(value, (end / start - 1) * 100)
        self.assertGreater(value, 0)

    def test_partial_window_is_not_a_shorter_factor(self):
        for length in (0, 100, MOMENTUM_LOOKBACK_BARS):
            self.assertIsNone(self.engine.calculate_momentum_12_1(
                pd.Series(np.linspace(1, 2, length))))

    def test_analysis_exposes_momentum_when_history_allows(self):
        short = self.engine.analyze_stock(prices(np.linspace(80, 100, 80)))
        self.assertIsNone(short['momentum_12_1'])
        long = self.engine.analyze_stock(prices(np.linspace(80, 100, 300)))
        self.assertIsNotNone(long['momentum_12_1'])

    def indicator_frame(self, macd_now):
        return pd.DataFrame({'close': [100, 101], 'sma_20': [10, 10], 'sma_50': [9, 9],
                             'rsi': [50, 50], 'macd': [1, macd_now], 'macd_signal': [0, 0],
                             'bb_upper': [200, 200], 'bb_lower': [1, 1], 'stoch_k': [50, 50],
                             'volume': [10, 10], 'volume_sma_20': [10, 10]})

    def test_moving_average_votes_count_once(self):
        # ma_crossover and trend both read price against a moving average.
        # Counting them separately let one input outvote MACD 2-1 every time.
        signals = self.engine._generate_signals(self.indicator_frame(-1))
        self.assertEqual(signals['ma_crossover'], 'bullish')
        self.assertEqual(signals['trend'], 'bullish')
        self.assertEqual(signals['macd'], 'bearish_cross')
        self.assertEqual(signals['overall'], 'neutral')

    def test_agreeing_votes_still_resolve(self):
        self.assertEqual(self.engine._generate_signals(
            self.indicator_frame(1))['overall'], 'bullish')

    def test_momentum_falls_back_to_trend_without_a_full_year(self):
        score, reasons = _score_momentum({'signals': {'overall': 'bullish'}}, {})
        self.assertEqual(score, 75)
        self.assertIn('no 12-1 momentum: needs ~12 months of history', reasons)


class CostHurdleAndHysteresis(unittest.TestCase):
    def screen(self, args, **kwargs):
        results, funds, scores, validations = args
        return build_candidate_list(results, funds, scores, validations=validations,
                                    as_of='2026-09-21', **kwargs)

    def test_breakeven_is_the_full_round_trip(self):
        expected = ((1.015 * 1.001) / (0.985 * 0.999) - 1) * 100
        self.assertAlmostEqual(round_trip_breakeven_pct(0.015, 0.001), expected, places=3)
        self.assertEqual(round_trip_breakeven_pct(0, 0), 0)
        self.assertGreater(round_trip_breakeven_pct(), 3)

    def test_breakeven_rejects_impossible_costs(self):
        for fee, slip in [(1, 0), (-.1, 0), (0, 1), (0, float('nan'))]:
            with self.assertRaises(ValueError):
                round_trip_breakeven_pct(fee, slip)

    def test_every_candidate_publishes_its_hurdle(self):
        candidate = self.screen(eligible())[0]
        self.assertAlmostEqual(candidate['breakeven_pct'], round_trip_breakeven_pct())
        self.assertEqual(candidate['action'], 'buy')
        self.assertFalse(candidate['held'])

    def test_track_record_publishes_the_same_hurdle(self):
        record = compute_track_record({})
        self.assertEqual(record['horizon_days'], DEFAULT_HORIZON_DAYS)
        self.assertAlmostEqual(record['breakeven_pct'], round_trip_breakeven_pct())
        self.assertIn('round trip costs', record['note'])

    def test_default_horizon_is_long_enough_to_clear_costs(self):
        # A 5- or 10-session hold asks the signal for ~100%/year in friction.
        self.assertGreaterEqual(DEFAULT_HORIZON_DAYS, 60)

    def test_held_name_survives_between_hold_and_entry_score(self):
        args = eligible(score=DEFAULT_HOLD_SCORE)
        self.assertEqual(self.screen(args), [])
        kept = self.screen(args, holdings={'AAA'})
        self.assertEqual([c['symbol'] for c in kept], ['AAA'])
        self.assertEqual(kept[0]['action'], 'hold')
        self.assertTrue(kept[0]['held'])
        self.assertEqual(kept[0]['min_score_applied'], DEFAULT_HOLD_SCORE)

    def test_held_name_is_dropped_once_it_breaks_the_dead_band(self):
        args = eligible(score=DEFAULT_HOLD_SCORE - 1)
        self.assertEqual(self.screen(args, holdings={'AAA'}), [])

    def test_held_name_is_not_rotated_out_by_the_technical_gauge(self):
        args = eligible(rating=0.0)
        self.assertEqual(self.screen(args), [])
        self.assertEqual(len(self.screen(args, holdings={'AAA'})), 1)

    def test_hysteresis_never_waives_data_integrity(self):
        for idx, key, value in [(0, 'identity_verified', False), (0, 'history_complete', False),
                                (3, 'status', 'stale'), (0, 'median_value_traded_20d', 0)]:
            args = copy.deepcopy(eligible(score=DEFAULT_HOLD_SCORE))
            args[idx]['AAA'][key] = value
            with self.subTest(key=key):
                self.assertEqual(self.screen(args, holdings={'AAA'}), [])

    def test_hold_threshold_cannot_exceed_the_entry_threshold(self):
        with self.assertRaises(ValueError):
            self.screen(eligible(), hold_score=DEFAULT_MIN_SCORE + 1)

    def test_exclusion_names_the_threshold_that_applied(self):
        reasons = {}
        self.screen(eligible(score=DEFAULT_HOLD_SCORE - 1), exclusions=reasons,
                    holdings={'AAA'})
        self.assertIn('score below hold minimum or missing', reasons['AAA'])
        reasons = {}
        self.screen(eligible(score=DEFAULT_MIN_SCORE - 1), exclusions=reasons)
        self.assertIn('score below entry minimum or missing', reasons['AAA'])


class HoldingsReviewWiring(unittest.TestCase):
    """The analyzer never sees holdings, so retention is applied app-side."""

    def review(self, positions, data):
        from portfolio.lambda_handler import _holdings_review
        return _holdings_review(positions, data)

    def test_retention_list_evaluates_every_name_as_if_held(self):
        # What the analyzer publishes as hold_candidates: the same screen with
        # the hold threshold applied, since it cannot know who owns what.
        args = eligible(score=DEFAULT_HOLD_SCORE, rating=0.0)
        self.assertEqual(build_candidate_list(args[0], args[1], args[2],
                                              validations=args[3], as_of='2026-09-21'), [])
        retained = build_candidate_list(args[0], args[1], args[2], validations=args[3],
                                        as_of='2026-09-21', holdings=set(args[0]))
        self.assertEqual([c['symbol'] for c in retained], ['AAA'])

    def test_held_name_on_the_retention_list_is_kept(self):
        keep, review = self.review({'AAA': {'qty': 10}},
                                   {'hold_candidates': [{'symbol': 'AAA', 'score': 50}]})
        self.assertEqual([s for s, _ in keep], ['AAA'])
        self.assertEqual(review, [])

    def test_held_name_off_the_retention_list_is_flagged(self):
        keep, review = self.review({'AAA': {'qty': 10}},
                                   {'hold_candidates': [{'symbol': 'BBB', 'score': 90}]})
        self.assertEqual(keep, [])
        self.assertEqual([s for s, _ in review], ['AAA'])

    def test_closed_positions_are_not_reviewed(self):
        keep, review = self.review({'AAA': {'qty': 0}}, {'hold_candidates': []})
        self.assertEqual((keep, review), ([], []))

    def test_hold_only_name_never_reaches_the_allocator(self):
        # The allocator is fed the entry-grade list only. A name kept by
        # hysteresis appears on the retention list and nowhere else, so it
        # cannot quietly absorb a fresh contribution.
        args = eligible(score=DEFAULT_HOLD_SCORE)
        entry = build_candidate_list(args[0], args[1], args[2], validations=args[3],
                                     as_of='2026-09-21')
        retained = build_candidate_list(args[0], args[1], args[2], validations=args[3],
                                        as_of='2026-09-21', holdings=set(args[0]))
        self.assertEqual([c['symbol'] for c in retained], ['AAA'])
        self.assertNotIn('AAA', [c['symbol'] for c in entry])
        self.assertEqual(allocate_budget(entry, 100_000), ([], 100_000))


def ic_sessions(start='2026-09-01', count=40):
    day = parse_date_or_die(start)
    days = [day.isoformat()]
    for _ in range(count - 1):
        day = next_session(day)
        days.append(day.isoformat())
    return days


def parse_date_or_die(value):
    from data_quality import parse_date, is_session
    day = parse_date(value)
    while not is_session(day):
        day = next_session(day)
    return day


def ic_snapshots(n_names=12, count=40, price=None, investable=None):
    """Snapshots whose forward returns are a known function of score."""
    price = price or (lambda i, k: 100.0 * (1 + i * 0.001) ** k)
    days = ic_sessions(count=count)
    snaps = {}
    for k, day in enumerate(days):
        snaps[day] = {
            f'S{i:02d}': {'score': float(i * 5), 'schema_version': 2,
                          'investable': True if investable is None else investable(i),
                          'selected': False, 'price': price(i, k),
                          'price_date': day, 'price_verified': True}
            for i in range(n_names)
        }
    return snaps


class InformationCoefficient(unittest.TestCase):
    def test_rank_correlation_basics(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)
        # Ties are averaged, so equal scores cannot invent an ordering.
        self.assertAlmostEqual(spearman([1, 1, 2, 2], [5, 5, 9, 9]), 1.0)
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))
        self.assertIsNone(spearman([1, 2], [1, 2]))
        self.assertIsNone(spearman([1, 2, 3], [1, 2]))

    def test_perfect_ordering_scores_one(self):
        result = compute_ic(ic_snapshots(), horizon_days=5)
        self.assertGreater(result['independent']['n'], 0)
        self.assertEqual(result['overlapping']['mean_ic'], 1.0)
        self.assertEqual(result['independent']['mean_ic'], 1.0)
        self.assertEqual(result['independent']['positive_rate'], 100.0)

    def test_inverted_ordering_scores_minus_one(self):
        snaps = ic_snapshots(price=lambda i, k: 100.0 * (1 - i * 0.001) ** k)
        self.assertEqual(compute_ic(snaps, horizon_days=5)['overlapping']['mean_ic'], -1.0)

    def test_uses_the_whole_investable_cross_section_not_the_buy_list(self):
        # Breadth is the entire point: one observation should use every
        # eligible name, not the five that were bought.
        result = compute_ic(ic_snapshots(n_names=30), horizon_days=5)
        self.assertEqual(result['observations'][0]['n_names'], 30)

    def test_non_investable_names_are_excluded(self):
        snaps = ic_snapshots(n_names=30, investable=lambda i: i < 20)
        self.assertEqual(compute_ic(snaps, horizon_days=5)['observations'][0]['n_names'], 20)

    def test_thin_cross_sections_are_skipped_not_averaged_in(self):
        result = compute_ic(ic_snapshots(n_names=MIN_NAMES - 1), horizon_days=5)
        self.assertEqual(result['overlapping']['n'], 0)
        self.assertGreater(result['skipped']['too_few_names'], 0)

    def test_a_missing_quote_drops_only_that_name(self):
        snaps = ic_snapshots(n_names=30)
        for day in list(snaps):
            for symbol in ('S00', 'S01'):
                snaps[day][symbol]['price_verified'] = False
        observation = compute_ic(snaps, horizon_days=5)['observations'][0]
        self.assertEqual(observation['dropped'], 2)
        self.assertEqual(observation['n_names'], 28)

    def test_wholesale_missing_quotes_invalidate_the_date(self):
        # Dropping names that lost their quote biases the result upward,
        # because suspensions and delistings are not random.
        snaps = ic_snapshots(n_names=30)
        cutoff = int(30 * MAX_DROPPED_FRACTION) + 2
        for day in list(snaps):
            for i in range(cutoff):
                snaps[day][f'S{i:02d}']['price_verified'] = False
        result = compute_ic(snaps, horizon_days=5)
        self.assertEqual(result['overlapping']['n'], 0)
        self.assertGreater(result['skipped']['too_many_dropped'], 0)

    def test_quote_must_come_from_the_session_it_claims(self):
        snaps = ic_snapshots(n_names=30)
        for day in list(snaps):
            for i in range(3):
                snaps[day][f'S{i:02d}']['price_date'] = '2020-01-02'
        self.assertEqual(compute_ic(snaps, horizon_days=5)['observations'][0]['dropped'], 3)

    def test_identical_scores_carry_no_ordering(self):
        snaps = ic_snapshots()
        for day in list(snaps):
            for record in snaps[day].values():
                record['score'] = 50.0
        result = compute_ic(snaps, horizon_days=5)
        self.assertEqual(result['overlapping']['n'], 0)
        self.assertGreater(result['skipped']['no_variation'], 0)

    def test_independent_subset_reserves_the_horizon(self):
        result = compute_ic(ic_snapshots(count=40), horizon_days=5)
        independent = result['independent']['n']
        self.assertLess(independent, result['overlapping']['n'])
        self.assertGreater(independent, 1)
        chosen = []
        reserved = None
        for obs in result['observations']:
            if reserved is None or obs['signal_date'] >= reserved:
                chosen.append(obs)
                reserved = obs['exit_date']
        self.assertEqual(len(chosen), independent)

    def test_significance_is_reported_only_for_independent_observations(self):
        # Overlapping windows share most of their return path, so a t-statistic
        # over them would overstate confidence several-fold.
        snaps = ic_snapshots(n_names=20, price=lambda i, k: 100.0 + i + ((i * 7 + k * 13) % 11))
        result = compute_ic(snaps, horizon_days=5)
        self.assertIsNone(result['overlapping']['t_stat'])
        self.assertGreater(result['overlapping']['n'], result['independent']['n'])
        if result['independent']['n'] > 1 and result['independent']['sd']:
            self.assertIsNotNone(result['independent']['t_stat'])

    def test_noiseless_sample_projects_no_horizon_estimate(self):
        # Every IC is exactly 1.0, so there is no dispersion to extrapolate.
        result = compute_ic(ic_snapshots(count=40), horizon_days=5)
        self.assertIsNone(result['independent']['years_to_significance'])

    def test_outstanding_periods_are_not_failures(self):
        result = compute_ic(ic_snapshots(count=6), horizon_days=60)
        self.assertEqual(result['overlapping']['n'], 0)
        self.assertEqual(sum(result['skipped'].values()), 0)

    def test_empty_history_is_not_an_error(self):
        result = compute_ic({}, horizon_days=5)
        self.assertEqual(result['overlapping']['n'], 0)
        self.assertIsNone(result['as_of'])
        self.assertIsNone(result['independent']['mean_ic'])

    def test_horizon_must_be_a_positive_integer(self):
        for bad in (0, -5, 2.5, 'five'):
            with self.assertRaises(ValueError):
                compute_ic(ic_snapshots(), horizon_days=bad)

    def test_decay_profile_covers_every_horizon(self):
        decay = compute_ic_decay(ic_snapshots(count=40), horizons=(5, 10))
        self.assertEqual(sorted(decay), [5, 10])
        self.assertEqual(decay[5]['horizon_days'], 5)
        self.assertGreater(decay[5]['independent']['n'], decay[10]['independent']['n'])

    def test_entry_convention_matches_the_portfolio_evaluation(self):
        result = compute_ic(ic_snapshots(count=40), horizon_days=5)
        obs = result['observations'][0]
        self.assertEqual(obs['entry_date'], next_session(parse_date_or_die(obs['signal_date'])).isoformat())
        self.assertEqual(obs['exit_date'],
                         next_session(parse_date_or_die(obs['entry_date']), 5).isoformat())

    def test_track_record_page_shows_the_cross_sectional_read(self):
        from report_generator import ReportGenerator
        generator = ReportGenerator.__new__(ReportGenerator)
        decay = compute_ic_decay(ic_snapshots(count=40), horizons=(5, 10))
        html = generator._build_track_record_body(ic_decay=decay)
        self.assertIn('information coefficient', html)
        self.assertIn('not a return', html)
        self.assertIn('5 trading days', html)
        self.assertIn('+1.000', html)

    def test_empty_ic_table_explains_itself(self):
        # A grid of dashes with no explanation is worse than no table.
        from report_generator import ReportGenerator
        generator = ReportGenerator.__new__(ReportGenerator)
        empty = compute_ic_decay(ic_snapshots(count=3), horizons=(5, 60))
        html = generator._build_track_record_body(ic_decay=empty)
        self.assertIn('No completed periods yet', html)
        self.assertIn('6 for the 5-session read', html)
        self.assertIn('61 for the 60-session read', html)

    def test_populated_ic_table_drops_the_pending_note(self):
        from report_generator import ReportGenerator
        generator = ReportGenerator.__new__(ReportGenerator)
        html = generator._build_track_record_body(
            ic_decay=compute_ic_decay(ic_snapshots(count=40), horizons=(5,)))
        self.assertNotIn('No completed periods yet', html)

    def test_archive_panel_is_labelled_as_a_diagnostic(self):
        from report_generator import ReportGenerator
        generator = ReportGenerator.__new__(ReportGenerator)
        days = ic_sessions(count=30)
        mined = snapshots_from_archive({
            d: {f'S{i:02d}': {'price': 100.0 * (1 + i * 0.001) ** k,
                              'overall': 'bullish' if i % 2 else 'bearish'}
                for i in range(20)}
            for k, d in enumerate(days)})
        decay = compute_ic_decay(mined, horizons=(5,), verified_only=False)
        html = generator._build_track_record_body(ic_archive=decay)
        self.assertIn('Archived technical signal', html)
        self.assertIn('Mined history', html)
        self.assertIn('not the current screen', html)
        self.assertIn('without look-ahead', html)

    def test_track_record_page_without_ic_says_so(self):
        from report_generator import ReportGenerator
        generator = ReportGenerator.__new__(ReportGenerator)
        html = generator._build_track_record_body()
        self.assertIn('information coefficient', html)
        self.assertIn('Not available on this run.', html)

    def test_archive_converts_the_bullish_bearish_call_to_an_ordering(self):
        archive = {'2026-09-01': {'AAA': {'price': 10., 'overall': 'bullish'},
                                  'BBB': {'price': 20., 'overall': 'bearish'},
                                  'CCC': {'price': 30., 'overall': 'sideways'},
                                  'DDD': {'price': 0., 'overall': 'bullish'},
                                  'EEE': {'price': None, 'overall': 'bearish'}}}
        rows = snapshots_from_archive(archive)['2026-09-01']
        self.assertEqual(sorted(rows), ['AAA', 'BBB'])
        self.assertEqual(rows['AAA']['score'], 1.0)
        self.assertEqual(rows['BBB']['score'], 0.0)
        self.assertEqual(rows['AAA']['price_provenance'], 'archive')
        self.assertEqual(snapshots_from_archive({}), {})
        self.assertEqual(snapshots_from_archive(None), {})

    def test_archive_records_are_rejected_by_the_strict_default(self):
        # Mined history has no independent cross-check, so it must never be
        # measured as though it did without the caller saying so.
        days = ic_sessions(count=30)
        snaps = snapshots_from_archive({
            d: {f'S{i:02d}': {'price': 100.0 * (1 + i * 0.001) ** k,
                              'overall': 'bullish' if i % 2 else 'bearish'}
                for i in range(20)}
            for k, d in enumerate(days)})
        self.assertEqual(compute_ic(snaps, horizon_days=5)['overlapping']['n'], 0)
        relaxed = compute_ic(snaps, horizon_days=5, verified_only=False)
        self.assertGreater(relaxed['overlapping']['n'], 0)

    def test_relaxed_provenance_is_stated_in_the_note(self):
        snaps = ic_snapshots()
        self.assertNotIn('PROVENANCE', compute_ic(snaps, horizon_days=5)['note'])
        self.assertIn('PROVENANCE',
                      compute_ic(snaps, horizon_days=5, verified_only=False)['note'])

    def test_relaxed_provenance_still_rejects_a_mismatched_date(self):
        snaps = ic_snapshots(n_names=30)
        for day in list(snaps):
            for i in range(3):
                snaps[day][f'S{i:02d}']['price_date'] = '2020-01-02'
        result = compute_ic(snaps, horizon_days=5, verified_only=False)
        self.assertEqual(result['observations'][0]['dropped'], 3)

    def test_investable_filter_can_be_waived_for_mined_history(self):
        snaps = ic_snapshots(n_names=30, investable=lambda i: False)
        self.assertEqual(compute_ic(snaps, horizon_days=5)['overlapping']['n'], 0)
        waived = compute_ic(snaps, horizon_days=5, require_investable=False)
        self.assertEqual(waived['observations'][0]['n_names'], 30)

    def test_note_states_what_an_ic_is_not(self):
        note = compute_ic(ic_snapshots(), horizon_days=5)['note']
        self.assertIn('not a return', note)
        self.assertIn('excluding dividends', note)
