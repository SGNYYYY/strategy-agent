import akshare as ak
import pandas as pd
import logging
import time

class NewsClient:
    def get_stock_news(self, ts_code, limit=5):
        """
        获取个股新闻
        :param ts_code: 股票代码 (e.g. 600519.SH)
        :param limit: 获取最近N条
        :return: string (formatted news summary)
        """
        try:
            # 清洗代码: 600519.SH -> 600519
            symbol = ts_code.split('.')[0]
            logging.info(f"Fetching news for {symbol} ({ts_code}) via AkShare...")
            
            # 使用 akshare 获取个股新闻 (东方财富源)
            df = ak.stock_news_em(symbol=symbol)
            
            if df is None or df.empty:
                return "No recent news found."

            # 按时间降序
            # Akshare返回列名通常为: 关键词, 新闻标题, 新闻内容, 发布时间, 文章链接
            if '发布时间' in df.columns:
                 df['发布时间'] = pd.to_datetime(df['发布时间'])
                 df = df.sort_values(by='发布时间', ascending=False)
            
            # 取最近的N条
            recent_news = df.head(limit)
            
            news_context = ""
            for index, row in recent_news.iterrows():
                title = row.get('新闻标题', 'No Title')
                date = row.get('发布时间', '')
                content = str(row.get('新闻内容', ''))[:100] # 截取前100字作为摘要
                news_context += f"- [{date}] {title}: {content}...\n"
            
            return news_context

        except Exception as e:
            logging.error(f"Failed to fetch news for {ts_code}: {e}")
            return "Error fetching news."

if __name__ == "__main__":
    client = NewsClient()
    print(client.get_stock_news("600519.SH"))
