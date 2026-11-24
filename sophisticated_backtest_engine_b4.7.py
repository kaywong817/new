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
##### Sophisticated Crypto Backtesting Engine v4.6 #####
########################################################

CONFIG = {
    'mode': 'bulk', # 'bulk' or 'specific'
    # 'mode': 'specific',  
    'file': 'data_pull_btc_24h_fullsample.csv',
    'fee': 0.0005,
    
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
        
    def load_and_preprocess_data(self, timeframe=None):
        """Load and preprocess data with configurable transformations"""
        df = pl.read_csv(self.config['file'], truncate_ragged_lines=True, infer_schema_length=10000)

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
    
    def run(self):
        """Main execution method"""
        if self.config['mode'] == 'bulk':
            return self.run_bulk_optimization()
        elif self.config['mode'] == 'specific':
            return self.run_specific_backtest()
        else:
            raise ValueError("Mode must be 'bulk' or 'specific'")

# Main execution
if __name__ == "__main__":
    engine = BacktestEngine(CONFIG)
    results = engine.run()
