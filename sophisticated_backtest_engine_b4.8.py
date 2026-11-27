# from turtle import mode
import polars as pl
import numpy as np
import plotly.express as px
import plotly.subplots as sp
import plotly.graph_objects as go
import pandas as pd
from pathlib import Path
import time

########################################################
##### Sophisticated Crypto Backtesting Engine v4.8 #####
########################################################

CONFIG = {
    'mode': 'walk_forward', # 'walk_forward', 'bulk', or 'specific'

    'file': 'data_pull_btc_24h_fullsample.csv',  # legacy bulk/specific modes
    'walk_forward_train_file': 'data_pull_btc_24h_insample.csv',
    'walk_forward_holdout_file': 'data_pull_btc_24h_outsample.csv',
    'walk_forward_train_bars': 730,  # two years of daily BTC bars
    'walk_forward_test_bars': 180,  # six-month test blocks
    'walk_forward_freq': '1D',
    'walk_forward_init_cash': 1.0,
    'walk_forward_evaluate_final_holdout': True,
    'fee': 0.0005,
    'slippage': 0.0,  # Add realistic slippage before interpreting results

    # Data manipulation settings
    'value_shift': 1,  # Shift value column by N periods (for data delay adjustment)
    'apply_pct_change': False,  # Apply percentage change to value column
    'pct_change_periods': 1,  # Periods for pct_change calculation

    # Post-shift value aggregation (applied after shift and pct_change)
    'apply_value_agg': False,          # True to enable post-shift aggregation
    'value_agg_type': 'rolling_mean',  # 'rolling_mean' or 'cum_sum'
    'value_agg_window': 3,             # Window N for the aggregation

    # Bulk mode settings
    'timeframes': ['24h'],# '12h', '8h', '4h', '3h', '2h'], # ,'1h_30', '30m'], # ,  , ,'20m', '10m'],
    'scalers': ['zscore', 'robust', 'minmax'],
    'strategies': ['ls', 'lo', 'so'],  # long-short, long-only, short-only
    'modes': [1, -1],  # momentum, reversion

    # Specific mode settings
    'specific_timeframe': '24h',
    'specific_scaler': 'zscore',
    'specific_strategy': 'ls',
    'specific_mode': 1, # 1=momentum, -1=reversion
    'specific_window': 70,
    'specific_threshold': 2,

    # Optimization settings
    'window_range': (10, 150, 10),
    'threshold_range': (0, 3, 0.2),
    'tplus': 1,  # Time shift for position
}

# Timeframe mapping for 10-minute raw data
TIMEFRAME_MAP = {
    '24h': {'hours': 24, 'minutes': [0], 'resolution': 1},  # 1 period per day
    '12h': {'hours': 12, 'minutes': [0], 'resolution': 2},  # 2 periods per day
    '8h': {'hours': 8, 'minutes': [0], 'resolution': 3},    # 3 periods per day
    '6h': {'hours': 6, 'minutes': [0], 'resolution': 4},    # 4 periods per day
    '4h': {'hours': 4, 'minutes': [0], 'resolution': 6},    # 6 periods per day
    '3h': {'hours': 3, 'minutes': [0], 'resolution': 8},    # 8 periods per day
    '2h': {'hours': 2, 'minutes': [0], 'resolution': 12},   # 12 periods per day
    '1h': {'hours': 1, 'minutes': [0], 'resolution': 24},   # 24 periods per day
    '1h_30': {'hours': 1, 'minutes': [30], 'resolution': 24}, # 1h intervals at xx:30 with correct 24 resolution
    '30m': {'hours': 1, 'minutes': [0, 30], 'resolution': 48}, # True 30m intervals (both xx:00 and xx:30)
    '20m': {'hours': 1, 'minutes': [0, 20, 40], 'resolution': 72}, # 72 periods per day
    '10m': {'hours': 1, 'minutes': [0, 10, 20, 30, 40, 50], 'resolution': 144}, # 144 periods per day (raw data)
}

pl.Config.set_tbl_rows(30)
pl.Config.set_tbl_cols(50)

class BacktestEngine:
    def __init__(self, config):
        self.config = config
        self.results_dir = Path("backtest_results")
        self.results_dir.mkdir(exist_ok=True)

    def _preprocess_frame(self, df, timeframe=None):
        """Apply configured causal transformations to an already-loaded frame."""
        df = df.sort('datetime')

        # Step 1: Apply value shift if configured
        if self.config['value_shift'] > 0:
            df = df.with_columns(pl.col('value').shift(self.config['value_shift']).alias('value'))

        # Step 2: Apply post-shift value aggregation on full data before any filtering
        if self.config['apply_value_agg']:
            n = self.config['value_agg_window']
            agg_type = self.config['value_agg_type']
            if agg_type == 'rolling_mean':
                df = df.with_columns(pl.col('value').rolling_mean(n).alias('value'))
            elif agg_type == 'cum_sum':
                df = df.with_columns(pl.col('value').rolling_sum(n).alias('value'))

        # Step 3: Apply timeframe filtering if specified
        if timeframe:
            tf_config = TIMEFRAME_MAP[timeframe]
            df = df.with_columns(pl.col("datetime").str.to_datetime().alias("datetime_rounded"))
            df = df.filter(
                (pl.col("datetime_rounded").dt.hour() % tf_config['hours'] == 0) &
                (pl.col("datetime_rounded").dt.minute().is_in(tf_config['minutes']))
            )

        # Step 4: Apply percentage change if configured
        if self.config['apply_pct_change']:
            df = df.with_columns(pl.col('value').pct_change(self.config['pct_change_periods']).alias('value'))

        return df

    def load_and_preprocess_data(self, timeframe=None, file_path=None):
        """Load and preprocess data with configurable transformations."""
        source = file_path or self.config['file']
        df = pl.read_csv(source, truncate_ragged_lines=True, infer_schema_length=10000)
        return self._preprocess_frame(df, timeframe)

    def apply_scaler(self, df, scaler_type, window):
        """Apply different scaling methods"""
        if scaler_type == 'zscore':
            return (df
                   .with_columns(pl.col("value").rolling_mean(window).alias("ma"))
                   .with_columns(pl.col('value').rolling_std(window).alias('sd'))
                   .with_columns(((pl.col('value') - pl.col('ma')) / pl.col('sd')).alias('scaled_value'))
            )

        elif scaler_type == 'robust':
            return (df
                   .with_columns(pl.col("value").rolling_median(window_size=window).alias("value_rolling_median"))
                   .with_columns(pl.col('value').rolling_quantile(window_size=window, quantile=0.25, interpolation="linear").alias("rolling_quantile_0.25"))
                   .with_columns(pl.col("value").rolling_quantile(window_size=window, quantile=0.75, interpolation="linear").alias("rolling_quantile_0.75"))
                   .with_columns((pl.col("rolling_quantile_0.75") - pl.col("rolling_quantile_0.25")).alias("rolling_iqr"))
                   .with_columns(((pl.col("value") - pl.col("value_rolling_median")) / pl.col("rolling_iqr")).alias("scaled_value"))
            )

        elif scaler_type == 'minmax':
            return (df
                   .with_columns(pl.col("value").rolling_min(window_size=window).alias("value_rolling_min"))
                   .with_columns(pl.col("value").rolling_max(window_size=window).alias("value_rolling_max"))
                   .with_columns(((pl.col("value") - pl.col("value_rolling_min")) / (pl.col("value_rolling_max") - pl.col("value_rolling_min"))).alias("scaled_value"))
            )

    def generate_positions(self, df, strategy_type, mode, threshold, scaler_type):
        """Generate position signals based on strategy type, mode, and scaler type"""
        if scaler_type == 'minmax':
            if strategy_type == 'ls':
                pos_logic = (pl.when(pl.col("scaled_value") > threshold).then(pl.lit(mode))
                            .when(pl.col("scaled_value") < threshold).then(pl.lit(-1 * mode))
                            .otherwise(pl.lit(0)))
            elif strategy_type == 'lo':
                pos_logic = (pl.when(pl.col("scaled_value") > threshold).then(pl.lit(mode))
                            .when(pl.col("scaled_value") < threshold).then(pl.lit(0))
                            .otherwise(pl.lit(0)))
            elif strategy_type == 'so':
                pos_logic = (pl.when(pl.col("scaled_value") > threshold).then(pl.lit(0))
                            .when(pl.col("scaled_value") < threshold).then(pl.lit(-1 * mode))
                            .otherwise(pl.lit(0)))


        else:
            # For zscore and robust scalers (can have negative values)
            if strategy_type == 'ls':
                pos_logic = (pl.when(pl.col("scaled_value") > threshold).then(pl.lit(mode))
                            .when(pl.col("scaled_value") < -threshold).then(pl.lit(-1 * mode))
                            .otherwise(pl.lit(0)))
            elif strategy_type == 'lo':
                pos_logic = (pl.when(pl.col("scaled_value") > threshold).then(pl.lit(mode))
                            .when(pl.col("scaled_value") < -threshold).then(pl.lit(0))
                            .otherwise(pl.lit(0)))
            elif strategy_type == 'so':
                pos_logic = (pl.when(pl.col("scaled_value") > threshold).then(pl.lit(0))
                            .when(pl.col("scaled_value") < -threshold).then(pl.lit(-1 * mode))
                            .otherwise(pl.lit(0)))

        return df.with_columns(pos_logic.alias('pos'))

    def calculate_pnl(self, df):
        """Calculate PnL, trades, and performance metrics"""
        return (df
               .with_columns(pl.col('pos').shift(self.config['tplus']).alias('pos_1'))
               .with_columns((pl.col('pos_1') - pl.col('pos')).abs().alias('trade'))
               .with_columns((pl.col('trade') * -self.config['fee']).alias('cost'))
               .with_columns(pl.col('price').pct_change().alias('pct_chg'))
               .with_columns((pl.col('pos_1') * pl.col('pct_chg') + pl.col('cost')).alias('pnl'))
               .with_columns(pl.col('pnl').cum_sum().alias('cum_pnl'))
               .with_columns((-1 * (pl.col("cum_pnl").cum_max() - pl.col("cum_pnl"))).alias("dd"))
        )

    def calculate_metrics(self, df_result, timeframe):
        """Calculate performance metrics using correct resolution for filtered data"""
        # Get resolution from timeframe mapping (accounts for 10m raw data filtering)
        resolution = TIMEFRAME_MAP[timeframe]['resolution']

        annual_return = df_result['pnl'].mean() * (365 * resolution)
        try:
            sharpe = df_result['pnl'].mean() / df_result['pnl'].std() * ((365 * resolution)**0.5)
        except ZeroDivisionError:
            sharpe = 0

        total_trade = df_result['trade'].sum()
        total_periods = len(df_result)
        mdd = df_result['dd'].min()

        try:
            calmar = annual_return / abs(mdd)
        except ZeroDivisionError:
            calmar = 0

        return {
            'annual_return': annual_return,
            'sharpe': sharpe,
            'total_trade': total_trade,
            'total_periods': total_periods,
            'trade_perc': total_trade / total_periods if total_periods > 0 else 0,
            'mdd': mdd,
            'calmar': calmar
        }

    def optimize_single_combination(self, timeframe, scaler, strategy, mode):
        """Run optimization for a single combination"""
        df = self.load_and_preprocess_data(timeframe)
        window_values = np.arange(*self.config['window_range'])
        threshold_values = np.arange(*self.config['threshold_range'])

        results = []
        for window in window_values:
            df_scaled = self.apply_scaler(df, scaler, window)

            for threshold in threshold_values:
                df_pos = self.generate_positions(df_scaled, strategy, mode, threshold, scaler)
                df_result = self.calculate_pnl(df_pos)

                # Add buy and hold for comparison
                df_result = (df_result
                            .with_columns(pl.col('price').pct_change().alias('bnh_pnl'))
                            .with_columns(pl.col('bnh_pnl').cum_sum().alias('hold_cumu'))
                )

                metrics = self.calculate_metrics(df_result, timeframe)

                # Calculate buy-and-hold Sharpe
                resolution = TIMEFRAME_MAP[timeframe]['resolution']
                bnh_sharpe = df_result['bnh_pnl'].mean() / df_result['bnh_pnl'].std() * ((365 * resolution)**0.5)
                bnh_annual_return = df_result['bnh_pnl'].mean() * (365 * resolution)

                results.append(pl.DataFrame({
                    'timeframe': [timeframe],
                    'scaler': [scaler],
                    'strategy': [strategy],
                    'mode': [mode],
                    'window': [float(window)],
                    'threshold': [float(threshold)],
                    'annual_return': [float(metrics['annual_return'])],
                    'sharpe': [float(metrics['sharpe'])],
                    'trade_count': [metrics['total_trade']],
                    'total_periods': [metrics['total_periods']],
                    'mdd': [float(metrics['mdd'])],
                    'calmar': [float(metrics['calmar'])],
                    'bnh_sharpe': [float(bnh_sharpe)],
                    'bnh_annual_return': [float(bnh_annual_return)],
                }))

        return pl.concat(results)

    def _build_signal_tag(self):
        """Build a short tag describing the current signal-processing config"""
        shift_tag = f"shift{self.config['value_shift']}"
        if self.config['apply_value_agg']:
            agg_tag = f"{self.config['value_agg_type']}{self.config['value_agg_window']}"
        else:
            agg_tag = "agg_off"
        return f"{shift_tag}_{agg_tag}"

    def create_heatmap(self, results_df, timeframe, scaler, strategy, mode):
        """Create and save heatmap"""
        results_pd = results_df.to_pandas()
        data_table = results_pd.pivot(index='window', columns='threshold', values='sharpe')

        colorscale = [
            [0.0, "darkred"],
            [0.25, "lightcoral"],
            [0.5, "white"],
            [0.75, "lightgreen"],
            [1.0, "darkgreen"]
        ]

        fig = px.imshow(data_table, text_auto=True, aspect="auto",
                       color_continuous_scale=colorscale,
                       color_continuous_midpoint=0)

        mode_name = "momentum" if mode == 1 else "reversion"
        signal_tag = self._build_signal_tag()

        title = f"{timeframe}_{scaler}_{strategy}_{mode_name} | {signal_tag} | sharpe"
        fig.update_layout(title=title, title_x=0.5)

        # Save heatmap
        filename = f"heatmap_{timeframe}_{scaler}_{strategy}_{mode_name}_{signal_tag}.html"
        fig.write_html(self.results_dir / filename)
        print(f"Saved: {filename}")

        return fig

    def run_bulk_optimization(self):
        """Run optimization for all combinations"""
        print("Starting bulk optimization...")
        all_results = []

        total_combinations = len(self.config['timeframes']) * len(self.config['scalers']) * len(self.config['strategies']) * len(self.config['modes'])
        current = 0

        for timeframe in self.config['timeframes']:
            for scaler in self.config['scalers']:
                for strategy in self.config['strategies']:
                    for mode in self.config['modes']:
                        current += 1
                        print(f"Processing {current}/{total_combinations}: {timeframe} + {scaler} + {strategy} + {'momentum' if mode == 1 else 'reversion'}")

                        # Run optimization
                        results = self.optimize_single_combination(timeframe, scaler, strategy, mode)
                        all_results.append(results)

                        # Create heatmap
                        self.create_heatmap(results, timeframe, scaler, strategy, mode)

        # Combine all results and create ranking table
        combined_results = pl.concat(all_results)
        self.create_ranking_table(combined_results)

        print(f"\nBulk optimization complete! Results saved in {self.results_dir}")
        return combined_results

    def create_ranking_table(self, results_df):
        """Create ranking table by Sharpe ratio"""
        # Find best parameters for each combination
        best_results = (results_df
                       .group_by(['timeframe', 'scaler', 'strategy', 'mode'])
                       .agg(pl.col('sharpe').max().alias('best_sharpe'))
                       .join(results_df, on=['timeframe', 'scaler', 'strategy', 'mode'])
                       .filter(pl.col('sharpe') == pl.col('best_sharpe'))
                       .with_columns((pl.col('trade_count') / pl.col('total_periods') * 100).alias('trade_percentage'))
                       .select(['timeframe', 'scaler', 'strategy', 'mode', 'window', 'threshold',
                               'sharpe', 'annual_return', 'mdd', 'calmar', 'bnh_sharpe', 'bnh_annual_return','trade_count', 'total_periods', 'trade_percentage'])
                       .sort('sharpe', descending=True)
        )

        # Save ranking table
        signal_tag = self._build_signal_tag()
        # add a column Pass bnh to the best_results
        best_results = best_results.with_columns(pl.when(pl.col("sharpe") > pl.col("bnh_sharpe")).then(pl.lit("Yes")).otherwise(pl.lit("No")).alias("Pass_bnh"))
        best_results.write_csv(self.results_dir / f"ranking_by_sharpe_{signal_tag}.csv")
        print(f"Ranking table saved: ranking_by_sharpe_{signal_tag}.csv")
        print("\nTop 10 combinations by Sharpe ratio:")
        print(best_results.head(10))

        return best_results

    def run_specific_backtest(self):
        """Run detailed backtest for specific configuration"""
        timeframe = self.config['specific_timeframe']
        scaler = self.config['specific_scaler']
        strategy = self.config['specific_strategy']
        mode = self.config['specific_mode']
        window = self.config['specific_window']
        threshold = self.config['specific_threshold']

        print(f"Running specific backtest: {timeframe} + {scaler} + {strategy} + {'momentum' if mode == 1 else 'reversion'}")
        print(f"Parameters: window = {window}, threshold = {threshold}")

        # Load and process data
        df = self.load_and_preprocess_data(timeframe)
        df_scaled = self.apply_scaler(df, scaler, window)
        df_pos = self.generate_positions(df_scaled, strategy, mode, threshold, scaler)
        df_result = self.calculate_pnl(df_pos)

        # Add buy and hold benchmark
        df_result = (df_result
                    .with_columns(pl.col('price').pct_change().alias('bnh_pnl'))
                    .with_columns(pl.col('bnh_pnl').cum_sum().alias('hold_cumu'))
        )

        # Calculate metrics
        metrics = self.calculate_metrics(df_result, timeframe)

        # Calculate buy-and-hold Sharpe with correct resolution
        resolution = TIMEFRAME_MAP[timeframe]['resolution']
        bnh_sharpe = df_result['bnh_pnl'].mean() / df_result['bnh_pnl'].std() * ((365 * resolution)**0.5)

        print(f"\nPerformance Metrics:")
        print(f"Annual return: {metrics['annual_return']:.4f}")
        print(f"Sharpe ratio: {metrics['sharpe']:.4f}")
        print(f"Buy and Hold Sharpe: {bnh_sharpe:.4f}")
        print(f"MDD: {metrics['mdd']:.4f}")
        print(f"Calmar ratio: {metrics['calmar']:.4f}")
        print(f"Total trades: {metrics['total_trade']} / {metrics['total_periods']} = {metrics['trade_perc']:.2%}")

        # Create visualization
        self.create_equity_curve(df_result, timeframe, scaler, strategy, mode, window, threshold)

        # Save detailed results
        mode_name = "momentum" if mode == 1 else "reversion"
        signal_tag = self._build_signal_tag()
        output_file = f"detailed_backtest_{timeframe}_{scaler}_{strategy}_{mode_name}_{window}_{threshold}_{signal_tag}.csv"
        df_result.write_csv(self.results_dir / output_file)
        print(f"Detailed results saved: {output_file}")

        return df_result

    def create_equity_curve(self, df_result, timeframe, scaler, strategy, mode, window, threshold):
        """Create and save equity curve visualization"""
        fig = sp.make_subplots(rows=2, cols=1, row_heights=[0.7, 0.3])

        fig.add_trace(go.Scatter(x=df_result['datetime'], y=df_result['cum_pnl'],
                               mode='lines', name='Strategy Cumulative PnL'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_result['datetime'], y=df_result['hold_cumu'],
                               mode='lines', name='Buy and Hold Cumulative PnL',
                               line=dict(color='black')), row=1, col=1)

        fig.add_trace(go.Scatter(x=df_result['datetime'], y=df_result['dd'],
                               fill='tozeroy', mode='none', name='Drawdown',
                               fillcolor='rgba(255, 100, 100, 0.5)'), row=2, col=1)

        mode_name = "momentum" if mode == 1 else "reversion"
        signal_tag = self._build_signal_tag()
        title = f"{timeframe}_{scaler}_{strategy}_{mode_name}_{window}_{threshold} | {signal_tag}"
        fig.update_layout(title_text=title, title_x=0.5)

        # Save equity curve
        filename = f"equity_curve_{timeframe}_{scaler}_{strategy}_{mode_name}_{window}_{threshold}_{signal_tag}.html"
        fig.write_html(self.results_dir / filename)
        print(f"Equity curve saved: {filename}")

        fig.show()
        return fig

    @staticmethod
    def _datetime_index(df):
        import pandas as pd
        return pd.DatetimeIndex(pd.to_datetime(df.get_column('datetime').to_list()), name='datetime')

    def _candidate_position_matrix(self, df):
        """Build one target-exposure column per parameter candidate."""
        import pandas as pd

        index = self._datetime_index(df)
        values = {}
        metadata = {}
        candidate_number = 0
        windows = [int(x) for x in np.arange(*self.config['window_range'])]
        configured_thresholds = [float(x) for x in np.arange(*self.config['threshold_range'])]

        for scaler in self.config['scalers']:
            # A rolling min-max score is bounded to [0, 1]. Thresholds above
            # that range would create degenerate, effectively constant positions.
            thresholds = (
                [x for x in configured_thresholds if 0.0 <= x < 1.0]
                if scaler == 'minmax'
                else configured_thresholds
            )
            for window in windows:
                scaled = self.apply_scaler(df, scaler, window)
                for strategy in self.config['strategies']:
                    for mode in self.config['modes']:
                        for threshold in thresholds:
                            candidate_id = f'candidate_{candidate_number:05d}'
                            positions = self.generate_positions(
                                scaled, strategy, mode, threshold, scaler
                            ).get_column('pos').fill_null(0).to_numpy()
                            values[candidate_id] = np.nan_to_num(
                                positions, nan=0.0, posinf=0.0, neginf=0.0
                            )
                            metadata[candidate_id] = {
                                'scaler': scaler,
                                'strategy': strategy,
                                'mode': int(mode),
                                'window': window,
                                'threshold': threshold,
                            }
                            candidate_number += 1

        if not values:
            raise ValueError('The configured parameter grid is empty.')
        return pd.DataFrame(values, index=index), metadata

    def _positions_for_candidate(self, df, spec):
        """Generate one causal position series for a frozen parameter set."""
        import pandas as pd

        scaled = self.apply_scaler(df, spec['scaler'], int(spec['window']))
        positions = self.generate_positions(
            scaled,
            spec['strategy'],
            int(spec['mode']),
            float(spec['threshold']),
            spec['scaler'],
        ).get_column('pos').fill_null(0).to_numpy()
        positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)
        return pd.Series(positions, index=self._datetime_index(df), name='target_exposure')

    def _vectorbt_portfolio(self, vbt, close, targets, apply_execution_delay=True):
        """Run targets as portfolio weights; VectorBT charges costs on actual orders."""
        execution_delay = int(self.config['tplus']) - 1
        if execution_delay < 0:
            raise ValueError('Walk-forward mode requires tplus >= 1 (no look-ahead).')
        if apply_execution_delay and execution_delay:
            targets = targets.shift(execution_delay)
        targets = targets.fillna(0.0)
        return vbt.Portfolio.from_orders(
            close=close,
            size=targets,
            size_type='targetpercent',
            direction='both',
            fees=float(self.config['fee']),
            slippage=float(self.config.get('slippage', 0.0)),
            init_cash=float(self.config['walk_forward_init_cash']),
            freq=self.config['walk_forward_freq'],
        ), targets

    @staticmethod
    def _metric_value(metric, key=None):
        """Normalize VectorBT scalar/Series/DataFrame metrics to a Python float."""
        import pandas as pd

        if isinstance(metric, pd.DataFrame):
            if key is not None and key in metric.columns:
                return float(metric[key].iloc[0])
            if metric.size == 1:
                return float(metric.to_numpy().reshape(-1)[0])
            raise ValueError('Expected a scalar VectorBT metric.')
        if isinstance(metric, pd.Series):
            if key is not None and key in metric.index:
                return float(metric.loc[key])
            if len(metric) == 1:
                return float(metric.iloc[0])
            raise ValueError('Expected a scalar VectorBT metric.')
        return float(metric)

    def _select_best_candidate(self, vbt, train_df):
        """Optimize on training rows only and return best candidate plus its Sharpe."""
        import pandas as pd

        target_matrix, metadata = self._candidate_position_matrix(train_df)
        close = pd.Series(
            train_df.get_column('price').to_numpy(),
            index=target_matrix.index,
            name='close',
        )
        portfolio, _ = self._vectorbt_portfolio(vbt, close, target_matrix)
        scores = portfolio.sharpe_ratio()
        if not isinstance(scores, pd.Series):
            scores = pd.Series(np.asarray(scores).reshape(-1), index=target_matrix.columns)
        scores = scores.replace([np.inf, -np.inf], np.nan).dropna()
        if scores.empty:
            raise ValueError('No parameter candidate produced a finite training Sharpe ratio.')
        best_id = scores.idxmax()
        return metadata[best_id], float(scores.loc[best_id])

    def _evaluate_frozen_candidate(self, vbt, history_df, evaluation_df, spec):
        """Create rolling features with prior context; score only evaluation timestamps."""
        import pandas as pd

        eval_index = self._datetime_index(evaluation_df)
        # Include enough pre-test rows for rolling features and delayed execution.
        warmup = max(
            int(spec['window']),
            int(self.config['value_agg_window']) if self.config['apply_value_agg'] else 1,
            int(self.config['pct_change_periods']) if self.config['apply_pct_change'] else 1,
        ) + int(self.config['tplus']) + 2
        combined = pl.concat([history_df.tail(warmup), evaluation_df], how='vertical_relaxed')
        position = self._positions_for_candidate(combined, spec).reindex(eval_index)
        close = pd.Series(
            evaluation_df.get_column('price').to_numpy(),
            index=eval_index,
            name='close',
        )
        portfolio, executed_targets = self._vectorbt_portfolio(vbt, close, position)
        return portfolio, executed_targets

    def _portfolio_summary(self, portfolio, target_positions):
        """Extract core metrics without ranking on the test set."""
        returns = portfolio.returns()
        total_return = portfolio.total_return()
        sharpe = portfolio.sharpe_ratio()
        max_drawdown = portfolio.max_drawdown()
        changes = target_positions.diff().abs()
        if getattr(target_positions, 'ndim', 1) == 2:
            changes = changes.fillna(target_positions.abs().iloc[0])
            turnover = float(changes.sum().sum())
        else:
            changes = changes.fillna(abs(target_positions.iloc[0]))
            turnover = float(changes.sum())
        return {
            'total_return': self._metric_value(total_return),
            'sharpe': self._metric_value(sharpe),
            'max_drawdown': self._metric_value(max_drawdown),
            'turnover': turnover,
            'observations': int(len(returns)),
        }

    def _write_portfolio_daily(self, portfolio, target_positions, path):
        import pandas as pd

        returns = portfolio.returns()
        equity = portfolio.value()
        if isinstance(returns, pd.DataFrame):
            returns = returns.iloc[:, 0]
        if isinstance(equity, pd.DataFrame):
            equity = equity.iloc[:, 0]
        if isinstance(target_positions, pd.DataFrame):
            target_positions = target_positions.iloc[:, 0]
        result = pd.DataFrame({
            'target_exposure': target_positions,
            'return': returns,
            'equity': equity,
        })
        result.to_csv(path, index_label='datetime')

    def run_walk_forward(self):
        """Run cookbook-style rolling split optimization plus a final untouched holdout."""
        try:
            import pandas as pd
            import vectorbt as vbt
        except ImportError as exc:
            raise RuntimeError(
                'Walk-forward mode requires pandas and the open-source VectorBT package. '
                'Install them in the project environment before running v4.8.'
            ) from exc

        train_path = self.config['walk_forward_train_file']
        holdout_path = self.config['walk_forward_holdout_file']
        raw_train = pl.read_csv(train_path, truncate_ragged_lines=True, infer_schema_length=10000)
        raw_holdout = pl.read_csv(holdout_path, truncate_ragged_lines=True, infer_schema_length=10000)
        train_dates = raw_train.get_column('datetime').to_list()
        holdout_dates = raw_holdout.get_column('datetime').to_list()
        if not train_dates or not holdout_dates:
            raise ValueError('Train and holdout files must both contain rows.')
        if max(train_dates) >= min(holdout_dates):
            raise ValueError('Holdout dates must start strictly after the in-sample end date.')
        if len(set(train_dates)) != len(train_dates) or len(set(holdout_dates)) != len(holdout_dates):
            raise ValueError('Duplicate timestamps found in train or holdout data.')
        # Preprocess the joined chronology so shifts/rolling aggregations have valid
        # history at the train/holdout boundary. All configured transforms are causal.
        raw_all = pl.concat([raw_train, raw_holdout], how='vertical_relaxed').sort('datetime')
        all_df = self._preprocess_frame(raw_all, self.config.get('specific_timeframe', '24h'))

        raw_train_end = max(raw_train.get_column('datetime').to_list())
        train_df = all_df.filter(pl.col('datetime') <= raw_train_end)
        holdout_df = all_df.filter(pl.col('datetime') > raw_train_end)
        if train_df.height == 0 or holdout_df.height == 0:
            raise ValueError('Walk-forward train/holdout files must have non-empty, ordered date ranges.')
        if train_df.get_column('datetime').max() >= holdout_df.get_column('datetime').min():
            raise ValueError('The final holdout must begin after the in-sample period ends.')

        train_index = self._datetime_index(train_df)
        if train_index.has_duplicates or not train_index.is_monotonic_increasing:
            raise ValueError('Preprocessed timestamps must be unique and chronological.')
        timeframe = self.config.get('specific_timeframe', '24h')
        expected_seconds = 86400 / TIMEFRAME_MAP[timeframe]['resolution']
        if len(train_index) > 1:
            observed_seconds = float(np.median(np.diff(train_index.asi8) / 1e9))
            if observed_seconds > expected_seconds * 1.5:
                raise ValueError(
                    f"Observed median bar spacing ({observed_seconds:g}s) is too coarse "
                    f"for timeframe {timeframe} ({expected_seconds:g}s expected). "
                    'Filtering daily rows cannot create intraday bars.'
                )
        close_for_split = pd.Series(
            train_df.get_column('price').to_numpy(), index=train_index, name='close'
        )
        train_bars = int(self.config['walk_forward_train_bars'])
        test_bars = int(self.config['walk_forward_test_bars'])
        if train_bars <= 0 or test_bars <= 0:
            raise ValueError('Walk-forward train/test lengths must be positive bar counts.')

        # VectorBT's rolling_split returns all 1-bar-shifted windows. Select every
        # test_bars-th window so OOS blocks do not overlap and cover time forward.
        window_len = train_bars + test_bars
        ((_, train_indexes), (_, test_indexes)) = close_for_split.vbt.rolling_split(
            window_len=window_len,
            set_lens=(test_bars,),
            left_to_right=False,
        )
        if not test_indexes:
            raise ValueError('Insufficient in-sample history for one configured walk-forward split.')

        def frame_for_index(date_index):
            positions = train_index.get_indexer(date_index)
            if len(positions) == 0 or np.any(positions < 0):
                raise ValueError('VectorBT split dates do not align with the source frame.')
            if not np.all(np.diff(positions) == 1):
                raise ValueError('Expected contiguous date rows in each VectorBT split.')
            return train_df.slice(int(positions[0]), len(positions))

        selected_fold_ids = list(range(0, len(test_indexes), test_bars))
        fold_rows = []
        oos_target_parts = []
        oos_close_parts = []
        prior_test_dates = set()
        for fold_no, split_id in enumerate(selected_fold_ids, start=1):
            fit_dates = train_indexes[split_id]
            test_dates = test_indexes[split_id]
            if len(fit_dates) < train_bars or len(test_dates) != test_bars:
                continue
            duplicate_dates = prior_test_dates.intersection(test_dates)
            if duplicate_dates:
                raise ValueError('Selected walk-forward test blocks overlap.')
            prior_test_dates.update(test_dates)

            fit_df = frame_for_index(fit_dates)
            test_df = frame_for_index(test_dates)
            best_spec, train_sharpe = self._select_best_candidate(vbt, fit_df)
            wf_portfolio, wf_targets = self._evaluate_frozen_candidate(
                vbt, fit_df, test_df, best_spec
            )
            metrics = self._portfolio_summary(wf_portfolio, wf_targets)
            fold_rows.append({
                'fold': fold_no,
                'train_start': str(fit_dates[0]),
                'train_end': str(fit_dates[-1]),
                'test_start': str(test_dates[0]),
                'test_end': str(test_dates[-1]),
                'train_sharpe_selected': train_sharpe,
                **best_spec,
                **{f'test_{key}': value for key, value in metrics.items()},
            })
            oos_target_parts.append(wf_targets)
            oos_close_parts.append(pd.Series(
                test_df.get_column('price').to_numpy(), index=self._datetime_index(test_df)
            ))
            print(
                f"Fold {fold_no}: train {fit_dates[0].date()}..{fit_dates[-1].date()}, "
                f"test {test_dates[0].date()}..{test_dates[-1].date()}, "
                f"selected {best_spec}, test Sharpe {metrics['sharpe']:.3f}"
            )

        if not fold_rows:
            raise ValueError('No complete walk-forward folds were evaluated.')
        self.results_dir.mkdir(parents=True, exist_ok=True)
        fold_path = self.results_dir / 'walk_forward_fold_results_v4.8.csv'
        pl.DataFrame(fold_rows).write_csv(fold_path)

        # One continuous VectorBT OOS portfolio preserves positions and charges
        # turnover costs across re-optimization boundaries between test blocks.
        oos_targets = pd.concat(oos_target_parts).sort_index()
        oos_close = pd.concat(oos_close_parts).sort_index()
        if oos_targets.index.has_duplicates or oos_close.index.has_duplicates:
            raise ValueError('Walk-forward OOS timestamps must be unique.')
        oos_targets = oos_targets.reindex(oos_close.index)
        oos_portfolio, executed_oos_targets = self._vectorbt_portfolio(
            vbt, oos_close, oos_targets, apply_execution_delay=False
        )
        oos_summary = self._portfolio_summary(oos_portfolio, executed_oos_targets)
        pl.DataFrame([oos_summary]).write_csv(
            self.results_dir / 'walk_forward_oos_summary_v4.8.csv'
        )
        self._write_portfolio_daily(
            oos_portfolio, executed_oos_targets,
            self.results_dir / 'walk_forward_oos_daily_v4.8.csv'
        )

        print(f'Walk-forward folds saved: {fold_path}')
        print(f'Combined OOS summary: {oos_summary}')

        # Strict final holdout: select a fixed configuration on all in-sample data,
        # then apply it once to the separate out-of-sample file.
        if self.config.get('walk_forward_evaluate_final_holdout', True):
            holdout_spec, train_sharpe = self._select_best_candidate(vbt, train_df)
            holdout_portfolio, holdout_targets = self._evaluate_frozen_candidate(
                vbt, train_df, holdout_df, holdout_spec
            )
            holdout_summary = {
                'train_sharpe_selected': train_sharpe,
                **holdout_spec,
                **self._portfolio_summary(holdout_portfolio, holdout_targets),
            }
            pl.DataFrame([holdout_summary]).write_csv(
                self.results_dir / 'final_holdout_summary_v4.8.csv'
            )
            self._write_portfolio_daily(
                holdout_portfolio, holdout_targets,
                self.results_dir / 'final_holdout_daily_v4.8.csv'
            )
            print(f'Final holdout (frozen parameters): {holdout_summary}')
        return pl.DataFrame(fold_rows)

    def run(self):
        """Main execution method"""
        if self.config['mode'] == 'walk_forward':
            return self.run_walk_forward()
        elif self.config['mode'] == 'bulk':
            return self.run_bulk_optimization()
        elif self.config['mode'] == 'specific':
            return self.run_specific_backtest()
        else:
            raise ValueError("Mode must be 'walk_forward', 'bulk', or 'specific'")

# Main execution
if __name__ == "__main__":
    engine = BacktestEngine(CONFIG)
    results = engine.run()
