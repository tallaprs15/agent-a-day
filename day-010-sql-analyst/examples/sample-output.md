## Answer
The 5 countries with the highest total invoice revenue are the USA ($523.06), Canada ($303.96), France ($195.10), Brazil ($190.10), and Germany ($156.48). The top-spending customer in the top country (USA) is Richard Cunningham (Customer ID 26), with a total spend of $47.62.

## Data
Top 5 Countries by Revenue:
| Country | TotalRevenue |
| --- | --- |
| USA | 523.06 |
| Canada | 303.96 |
| France | 195.10 |
| Brazil | 190.10 |
| Germany | 156.48 |

Top Customer in the USA:
| CustomerId | FirstName | LastName | CustomerRevenue |
| --- | --- | --- | --- |
| 26 | Richard | Cunningham | 47.62 |

## SQL used
```sql
SELECT BillingCountry AS Country, SUM(Total) AS TotalRevenue FROM Invoice GROUP BY BillingCountry ORDER BY TotalRevenue DESC LIMIT 5
```
```sql
SELECT c.CustomerId, c.FirstName, c.LastName, SUM(i.Total) AS CustomerRevenue FROM Customer c JOIN Invoice i ON c.CustomerId = i.CustomerId WHERE c.Country = 'USA' GROUP BY c.CustomerId, c.FirstName, c.LastName ORDER BY CustomerRevenue DESC LIMIT 1
```

## Notes
- Revenue is aggregated using the `Total` column from the `Invoice` table, grouped by `BillingCountry` (which aligns with the customer's country).
- The top-spending customer in the USA was identified by joining `Customer` and `Invoice` tables, filtering for customers where `Country = 'USA'`, and ordering by their total invoice amount descending.